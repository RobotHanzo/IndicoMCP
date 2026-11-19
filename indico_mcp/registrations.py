from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from flask import request
from indico.core import signals
from indico.core.db import db
from indico.core.errors import IndicoError
from indico.modules.events import Event
from indico.modules.events.models.events import EventType
from indico.modules.events.payment import payment_settings
from indico.modules.events.payment.util import toggle_registration_payment
from indico.modules.events.registration.controllers.management.fields import _fill_form_field_with_data
from indico.modules.events.registration.controllers.management.regforms import _get_regform_creation_log_data
from indico.modules.events.registration.controllers.management.reglists import _modify_registration_status
from indico.modules.events.registration.fields import get_field_types
from indico.modules.events.registration.models.form_fields import RegistrationFormField
from indico.modules.events.registration.models.forms import ModificationMode, RegistrationForm
from indico.modules.events.registration.models.invitations import InvitationState, RegistrationInvitation
from indico.modules.events.registration.models.items import RegistrationFormItemType, RegistrationFormSection
from indico.modules.events.registration.models.registrations import (
    PublishRegistrationsMode,
    Registration,
    RegistrationState,
)
from indico.modules.events.registration.notifications import notify_registration_state_update
from indico.modules.events.registration.util import (
    clone_registration_form,
    close_registration,
    create_personal_data_fields,
    create_registration,
    get_event_regforms_registrations,
    get_flat_section_submission_data,
    get_form_registration_data,
    get_hidden_conditional_fields,
    get_initial_form_values,
    get_user_data,
    make_registration_schema,
    modify_registration,
    update_regform_item_positions,
)
from indico.modules.logs.models.entries import EventLogRealm, LogKind
from indico.modules.logs.util import make_diff_log
from indico.util.date_time import now_utc
from indico.util.signals import values_from_signal
from indico.util.string import snakify_keys
from indico.web.flask.util import url_for
from marshmallow import ValidationError
from pydantic import Field
from werkzeug.exceptions import BadRequest, Forbidden, NotFound

from indico_mcp.registry import ToolError

ID = Annotated[int, Field(ge=1, le=2_147_483_647)]
TITLE = Annotated[str, Field(min_length=1, max_length=200)]
FORM_DATA = Annotated[dict[str, object], Field(max_length=200)]
PAGE_SIZE = Annotated[int, Field(ge=1, le=100)]
PRICE = Annotated[str, Field(pattern=r'^\d{1,9}(\.\d{1,2})?$')]
TOKEN = Annotated[str, Field(min_length=32, max_length=36)]


def _event(event_id):
    event = Event.get(event_id)
    if event is None or event.is_deleted or not event.has_feature('registration'):
        raise NotFound
    return event


def _manager(user, event, *permissions, write=False):
    if not any(event.can_manage(user, permission=permission) for permission in ('registration', *permissions)):
        raise Forbidden
    if write and event.is_locked:
        raise BadRequest('The event is locked.')


def _form(event, form_id, *, lock=False):
    query = RegistrationForm.query.filter_by(id=form_id, event_id=event.id, is_deleted=False)
    if lock:
        query = query.enable_eagerloads(False).with_for_update(of=RegistrationForm)
    if (regform := query.one_or_none()) is None:
        raise NotFound
    return regform


def _registration_query(regform, manager=None):
    query = Registration.query.filter_by(registration_form_id=regform.id, is_deleted=False)
    if manager is not None:
        criteria = values_from_signal(signals.event.filter_registration_list.send(regform, user=manager), as_list=True)
        query = query.filter(*criteria)
    return query


def _registration(regform, registration_id, *, manager=None, lock=False):
    query = _registration_query(regform, manager).filter_by(id=registration_id)
    if lock:
        query = query.enable_eagerloads(False).with_for_update(of=Registration)
    if (registration := query.one_or_none()) is None:
        raise NotFound
    return registration


def _participant_access(user, event, regform, registration, *, form_token=None, invitation=None):
    if not event.can_access(user):
        can_register_for_access = (not event.access_key and event.public_regform_access
                                   and regform.in_event_acls.filter_by(event_id=event.id).has_rows())
        if not can_register_for_access and not (invitation and invitation.skip_access_check):
            raise Forbidden
    if regform.private and registration is None and invitation is None:
        if _parse_token(form_token) != str(regform.uuid):
            raise Forbidden


def _parse_token(token):
    if token is None:
        return None
    try:
        return str(UUID(token))
    except ValueError as exc:
        raise BadRequest('The supplied form or invitation token is invalid.') from exc


def _invitation(user, regform, token, *, lock=False):
    if token is None:
        return None
    query = RegistrationInvitation.query.filter_by(uuid=_parse_token(token), registration_form_id=regform.id)
    if lock:
        query = query.enable_eagerloads(False).with_for_update(of=RegistrationInvitation)
    invitation = query.one_or_none()
    if invitation is None or invitation.email.lower() not in _user_emails(user):
        raise NotFound
    return invitation


def _own_registration(user, event, regform, *, lock=False):
    registration = regform.get_registration(user=user)
    _participant_access(user, event, regform, registration)
    if lock and registration:
        return _registration(regform, registration.id, lock=True)
    return registration


def _form_result(regform, *, management=False):
    endpoint = 'event_registration.manage_regform' if management else 'event_registration.display_regform'
    result = {
        'id': regform.id,
        'event_id': regform.event_id,
        'title': regform.title,
        'open': regform.is_open,
        'active': regform.is_active,
        'start': regform.start_dt.isoformat() if regform.start_dt else None,
        'end': regform.end_dt.isoformat() if regform.end_dt else None,
        'modification_end': regform.modification_end_dt.isoformat() if regform.modification_end_dt else None,
        'currency': regform.currency,
        'base_price': str(regform.base_price),
        'url': url_for(endpoint, regform, _external=True),
    }
    if management:
        result.update(private=regform.private, registration_limit=regform.registration_limit,
                      require_login=regform.require_login, require_user=regform.require_user,
                      moderation_enabled=regform.moderation_enabled, modification_mode=regform.modification_mode.name)
    return result


def _registration_result(registration, *, management=False, detail=False):
    regform = registration.registration_form
    endpoint = 'event_registration.registration_details' if management else 'event_registration.display_regform'
    result = {
        'id': registration.id,
        'form_id': regform.id,
        'state': registration.state.name,
        'name': registration.full_name,
        'email': registration.email,
        'paid': registration.is_paid,
        'checked_in': registration.checked_in,
        'price': str(registration.price),
        'currency': registration.currency,
        'url': url_for(endpoint, registration if management else regform, _external=True),
    }
    if detail and not regform.is_purged:
        result['values'] = get_form_registration_data(regform, registration, management=management)
    return result


def _load_form(regform, values, *, registration=None, management=False):
    schema_cls = make_registration_schema(regform, management=management, registration=registration,
                                          override_required=management and registration is not None)
    # Registration schema signals use the resolved form ID in the native view args.
    view_args = request.view_args
    request.view_args = {**(view_args or {}), 'event_id': regform.event_id, 'reg_form_id': regform.id}
    # Match Indico's two-pass conditional-field validation on the MCP inputs.
    try:
        processed = schema_cls(partial=True).load(dict(values))
        if registration is None:
            field_values = {field.id: processed.get(field.html_field_name) for field in regform.active_fields}
            partial = ()
        else:
            field_values = {field_id: entry.data for field_id, entry in registration.data_by_field.items()}
            partial = True if management else [field.html_field_name for field in regform.active_fields
                                               if field.id in field_values]
            field_values.update({field.id: processed[field.html_field_name] for field in regform.active_fields
                                 if field.html_field_name in processed})
        hidden = {field.html_field_name for field in get_hidden_conditional_fields(regform, field_values)}
        return schema_cls(partial=partial, exclude=hidden).load(dict(values))
    except ValidationError as exc:
        known_fields = set(schema_cls().fields) | {'_schema'}
        errors = {key: value for key, value in exc.normalized_messages().items() if key in known_fields}
        raise ToolError('The registration form rejected the submitted values.', details=errors) from exc
    finally:
        request.view_args = view_args


def _user_emails(user):
    return {email.lower() for email in (user.email, *user.all_emails) if email}


def _require_own_email(user, values):
    if 'email' in values:
        email = values['email']
        if not isinstance(email, str) or email.strip().lower() not in _user_emails(user):
            raise BadRequest('Use an email address associated with your Indico profile.')


def _datetime(value):
    if value is None:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            raise ValueError('Timestamp has no offset')
        return dt.astimezone(UTC)
    except (ValueError, OverflowError) as exc:
        raise BadRequest('Use an ISO 8601 timestamp with a UTC offset.') from exc


def _confirmed(confirm):
    if not confirm:
        raise BadRequest('Set confirm to true after reviewing the action.')


def _save_field(field, input_type, title, description, required, options):
    data = snakify_keys(options or {})
    data.update(input_type=input_type, title=title.strip(), description=description, is_required=required)
    try:
        _fill_form_field_with_data(field, data)
    except ValidationError as exc:
        raise ToolError('The registration field settings are invalid.', details=exc.normalized_messages()) from exc


def register_tools(registry):
    @registry.tool(idempotent=True)
    def indico_list_registration_forms(user, event_id: ID, management: bool = False):
        """List registration forms for an event. Management mode includes private and unscheduled forms."""
        event = _event(event_id)
        if management:
            _manager(user, event)
            forms = RegistrationForm.query.filter_by(event_id=event.id, is_deleted=False).order_by(RegistrationForm.id)
        else:
            restricted = not event.can_access(user)
            if restricted and (event.access_key or not (event.has_regform_in_acl and event.public_regform_access)):
                raise Forbidden
            forms, _ = get_event_regforms_registrations(event, user, only_in_acl=restricted)
        return {'forms': [_form_result(form, management=management) for form in forms]}

    @registry.tool(idempotent=True)
    def indico_get_registration_form(user, event_id: ID, form_id: ID, management: bool = False,
                                     registration_id: ID | None = None, form_token: TOKEN | None = None,
                                     invitation_token: TOKEN | None = None):
        """Get registration fields, choice options, and initial values. Use field names as submission keys."""
        event = _event(event_id)
        if management:
            _manager(user, event, 'registration_edit')
        regform = _form(event, form_id)
        invitation = None
        if management:
            registration = (_registration(regform, registration_id, manager=user)
                            if registration_id is not None else None)
        else:
            if registration_id is not None:
                raise BadRequest('Omit registration_id to look up your own registration.')
            invitation = _invitation(user, regform, invitation_token)
            registration = regform.get_registration(user=user)
            _participant_access(user, event, regform, registration, form_token=form_token, invitation=invitation)
        if regform.is_purged:
            return {**_form_result(regform, management=management), 'purged': True}
        fields = get_flat_section_submission_data(regform, management=management, registration=registration)
        initial = (get_form_registration_data(regform, registration, management=management) if registration else
                   get_initial_form_values(regform, management=management) | get_user_data(regform, user, invitation))
        schema = make_registration_schema(regform, management=management, registration=registration)()
        return {
            **_form_result(regform, management=management),
            'form': fields,
            'initial_values': initial,
            'parameters': {name: {'required': field.required} for name, field in schema.fields.items()},
            'invitation_state': invitation.state.name if invitation else None,
        }

    @registry.tool(idempotent=True)
    def indico_get_registration(user, event_id: ID, form_id: ID, registration_id: ID | None = None):
        """Read your registration, or a specific registration with registration management permission."""
        event = _event(event_id)
        if registration_id is not None:
            _manager(user, event)
        regform = _form(event, form_id)
        registration = (_registration(regform, registration_id, manager=user) if registration_id is not None
                        else _own_registration(user, event, regform))
        if registration is None:
            raise NotFound
        return _registration_result(registration, management=registration_id is not None, detail=True)

    @registry.tool(idempotent=True)
    def indico_list_registrants(user, event_id: ID, form_id: ID, after_id: ID | None = None, limit: PAGE_SIZE = 30):
        """List registrations for a form, including contact and payment status, with registration management access."""
        event = _event(event_id)
        _manager(user, event)
        regform = _form(event, form_id)
        registrations = (_registration_query(regform, user)
                         .filter(Registration.id > (after_id or 0)).order_by(Registration.id).limit(limit + 1).all())
        return {
            'registrations': [_registration_result(reg, management=True) for reg in registrations[:limit]],
            'next_after_id': registrations[limit - 1].id if len(registrations) > limit else None,
        }

    @registry.tool(idempotent=True)
    def indico_get_registration_ticket_links(user, event_id: ID, form_id: ID):
        """Get the signed-in browser's PDF and available Wallet links for your ticket."""
        event = _event(event_id)
        regform = _form(event, form_id)
        registration = _own_registration(user, event, regform)
        # Ticket endpoints require ordinary event access even where form-only access exists.
        if (not event.can_access(user) or registration is None or registration.state != RegistrationState.complete
                or not regform.tickets_enabled or
                (not regform.ticket_on_event_page and not regform.ticket_on_summary_page
                 and not event.can_manage(user, permission='registration'))):
            raise Forbidden
        if regform.get_ticket_template().is_ticket and registration.is_ticket_blocked:
            raise Forbidden
        links = {'pdf': url_for('event_registration.ticket_download', regform, _external=True)}
        if regform.is_google_wallet_available:
            links['google_wallet'] = url_for('event_registration.ticket_google_wallet', regform, _external=True)
        if regform.is_apple_wallet_available:
            links['apple_wallet'] = url_for('event_registration.ticket_apple_wallet', regform, _external=True)
        return {'links': links}

    @registry.tool(read_only=False, open_world=True)
    def indico_submit_registration(user, event_id: ID, form_id: ID, values: FORM_DATA,
                                   management: bool = False, notify_user: bool = False,
                                   form_token: TOKEN | None = None, invitation_token: TOKEN | None = None):
        """Submit a registration as yourself or, with registration edit permission, for another user."""
        event = _event(event_id)
        if management:
            _manager(user, event, 'registration_edit', write=True)
        regform = _form(event, form_id, lock=True)
        if regform.is_purged:
            raise Forbidden
        invitation = None
        if not management:
            registration = regform.get_registration(user=user)
            invitation = _invitation(user, regform, invitation_token, lock=True)
            _participant_access(user, event, regform, registration, form_token=form_token, invitation=invitation)
            if registration or regform.limit_reached or (not regform.is_open and not invitation):
                raise BadRequest('You cannot register for this event.')
            if invitation and invitation.state == InvitationState.accepted:
                raise BadRequest('This invitation has already been accepted.')
            values = {'email': invitation.email if invitation else user.email, **values}
            if invitation and invitation.lock_email:
                values['email'] = invitation.email
            _require_own_email(user, values)
        data = _load_form(regform, values, management=management)
        if 'email' not in data:
            raise BadRequest('An email address is required.')
        reg = create_registration(regform, data, invitation=invitation, management=management,
                                  notify_user=notify_user or not management)
        return _registration_result(reg, management=management)

    @registry.tool(read_only=False, destructive=True, idempotent=True)
    def indico_decline_registration_invitation(user, event_id: ID, form_id: ID, invitation_token: TOKEN, confirm: bool):
        """Decline a pending registration invitation sent to an email on your Indico account."""
        _confirmed(confirm)
        event = _event(event_id)
        regform = _form(event, form_id, lock=True)
        invitation = _invitation(user, regform, invitation_token, lock=True)
        _participant_access(user, event, regform, None, invitation=invitation)
        if invitation.state == InvitationState.pending:
            invitation.state = InvitationState.declined
        return {'state': invitation.state.name}

    @registry.tool(read_only=False, open_world=True)
    def indico_update_registration(user, event_id: ID, form_id: ID, values: FORM_DATA,
                                   registration_id: ID | None = None, notify_user: bool = False):
        """Submit changed fields for your registration, or edit a specific registration as a manager."""
        event = _event(event_id)
        management = registration_id is not None
        if management:
            _manager(user, event, 'registration_edit', write=True)
        regform = _form(event, form_id, lock=True)
        reg = (_registration(regform, registration_id, manager=user, lock=True) if management
               else _own_registration(user, event, regform, lock=True))
        if reg is None or regform.is_purged or (not management and not reg.can_be_modified):
            raise Forbidden
        if not management:
            _require_own_email(user, values)
        data = _load_form(regform, values, registration=reg, management=management)
        modify_registration(reg, data, management=management, notify_user=notify_user or not management)
        return _registration_result(reg, management=management)

    @registry.tool(read_only=False, destructive=True, open_world=True)
    def indico_withdraw_registration(user, event_id: ID, form_id: ID, confirm: bool,
                                     registration_id: ID | None = None):
        """Withdraw your own registration or withdraw a registrant with registration moderation permission."""
        _confirmed(confirm)
        event = _event(event_id)
        management = registration_id is not None
        if management:
            _manager(user, event, 'registration_moderation', write=True)
        regform = _form(event, form_id, lock=True)
        reg = (_registration(regform, registration_id, manager=user, lock=True) if management
               else _own_registration(user, event, regform, lock=True))
        if reg is None or (not management and (not event.can_access(user) or not reg.can_be_withdrawn)):
            raise Forbidden
        if reg.state in (RegistrationState.withdrawn, RegistrationState.rejected):
            raise BadRequest('The registration cannot be withdrawn in its current state.')
        reg.update_state(withdrawn=True)
        notify_registration_state_update(reg, from_management=management)
        return _registration_result(reg, management=management)

    @registry.tool(read_only=False, destructive=True, open_world=True)
    def indico_moderate_registration(user, event_id: ID, form_id: ID, registration_id: ID,
                                     action: Literal['approve', 'reject', 'reset'], confirm: bool,
                                     reason: Annotated[str, Field(max_length=4000)] = '', attach_reason: bool = False):
        """Approve, reject, or reset a registration using the event's registration moderation permissions."""
        _confirmed(confirm)
        event = _event(event_id)
        _manager(user, event, 'registration_moderation', write=True)
        reg = _registration(_form(event, form_id, lock=True), registration_id, manager=user, lock=True)
        if action == 'reset':
            if reg.state == RegistrationState.pending:
                raise BadRequest('The registration is already pending.')
            try:
                reg.reset_state()
            except IndicoError as exc:
                raise BadRequest(str(exc)) from exc
        elif not _modify_registration_status(reg, approve=action == 'approve', rejection_reason=reason,
                                             attach_rejection_reason=attach_reason):
            raise BadRequest('The registration must be pending.')
        return _registration_result(reg, management=True)

    @registry.tool(read_only=False, idempotent=True)
    def indico_set_registration_check_in(user, event_id: ID, form_id: ID, registration_id: ID, checked_in: bool):
        """Mark a participant checked in or undo their check-in with registration check-in permission."""
        event = _event(event_id)
        _manager(user, event, 'registration_checkin', write=True)
        reg = _registration(_form(event, form_id), registration_id, manager=user, lock=True)
        if checked_in and reg.state not in (RegistrationState.complete, RegistrationState.unpaid):
            raise BadRequest('This registration cannot be marked as checked in.')
        if reg.checked_in != checked_in:
            reg.checked_in = checked_in
            signals.event.registration_checkin_updated.send(reg)
        return _registration_result(reg, management=True)

    @registry.tool(read_only=False, destructive=True, idempotent=True, open_world=True)
    def indico_set_registration_payment(user, event_id: ID, form_id: ID, registration_id: ID,
                                        paid: bool, confirm: bool):
        """Record or reverse an offline payment status as a registration manager."""
        _confirmed(confirm)
        event = _event(event_id)
        _manager(user, event, write=True)
        reg = _registration(_form(event, form_id, lock=True), registration_id, manager=user, lock=True)
        if reg.is_paid != paid:
            toggle_registration_payment(reg, paid=paid)
        return _registration_result(reg, management=True)

    @registry.tool(read_only=False, destructive=True)
    def indico_delete_registration(user, event_id: ID, form_id: ID, registration_id: ID, confirm: bool):
        """Soft-delete a registration using registration edit permission."""
        _confirmed(confirm)
        event = _event(event_id)
        _manager(user, event, 'registration_edit', write=True)
        reg = _registration(_form(event, form_id, lock=True), registration_id, manager=user, lock=True)
        reg.is_deleted = True
        signals.event.registration_deleted.send(reg, permanent=False)
        reg.log(EventLogRealm.management, LogKind.negative, 'Registration',
                f'Registration deleted: {reg.full_name}', user, data={'Email': reg.email})
        return {'id': reg.id, 'deleted': True}

    @registry.tool(read_only=False)
    def indico_create_registration_form(user, event_id: ID, title: TITLE, require_moderation: bool = False):
        """Create an unscheduled registration form with Indico's standard personal data fields."""
        event = _event(event_id)
        _manager(user, event, write=True)
        if not title.strip():
            raise BadRequest('The title cannot be blank.')
        form = RegistrationForm(event=event, title=title.strip(), currency=payment_settings.get('currency'),
                                moderation_enabled=require_moderation)
        form.publish_registrations_participants = (PublishRegistrationsMode.hide_all
                                                   if event.type_ == EventType.conference
                                                   else PublishRegistrationsMode.show_all)
        form.publish_registrations_public = PublishRegistrationsMode.hide_all
        create_personal_data_fields(form)
        db.session.add(form)
        db.session.flush()
        signals.event.registration_form_created.send(form)
        form.log(EventLogRealm.management, LogKind.positive, 'Registration',
                 f'Registration form "{form.title}" has been created', user, data=_get_regform_creation_log_data(form))
        return _form_result(form, management=True)

    @registry.tool(read_only=False, idempotent=True, destructive=True)
    def indico_update_registration_form(
        user, event_id: ID, form_id: ID, confirm: bool, title: TITLE | None = None,
        introduction: Annotated[str, Field(max_length=20000)] | None = None,
        contact_info: Annotated[str, Field(max_length=1000)] | None = None,
        private: bool | None = None, require_login: bool | None = None, require_user: bool | None = None,
        moderation_enabled: bool | None = None,
        registration_limit: Annotated[int, Field(ge=0, le=2_147_483_647)] | None = None,
        modification_mode: Literal['not_allowed', 'allowed_always', 'allowed_until_approved',
                                   'allowed_until_payment'] | None = None,
        base_price: PRICE | None = None, currency: Annotated[str, Field(min_length=1, max_length=10)] | None = None,
    ):
        """Update selected registration settings. Set registration_limit to 0 to remove the capacity limit."""
        _confirmed(confirm)
        event = _event(event_id)
        _manager(user, event, write=True)
        form = _form(event, form_id, lock=True)
        if form.is_purged:
            raise Forbidden
        if title is not None and not title.strip():
            raise BadRequest('The title cannot be blank.')
        if currency is not None and currency not in {c['code'] for c in payment_settings.get('currencies')}:
            raise BadRequest('The currency is unavailable for new registrations.')
        price = Decimal(base_price) if base_price is not None else None
        if price and not event.has_feature('payment'):
            raise BadRequest('Enable payment for the event before setting a registration fee.')
        proposed = {
            'title': title.strip() if title else None, 'introduction': introduction, 'contact_info': contact_info,
            'private': private, 'require_login': require_login, 'require_user': require_user,
            'moderation_enabled': moderation_enabled, 'registration_limit': registration_limit,
            'modification_mode': ModificationMode[modification_mode] if modification_mode else None,
            'base_price': price, 'currency': currency,
        }
        changes = {key: (getattr(form, key), None if key == 'registration_limit' and value == 0 else value)
                   for key, value in proposed.items() if value is not None}
        changes = {key: (previous, new) for key, (previous, new) in changes.items() if previous != new}
        for key, (_, new) in changes.items():
            setattr(form, key, new)
        if changes:
            log_data = make_diff_log(changes, {key: {'title': key.replace('_', ' ').capitalize()} for key in changes})
            form.log(EventLogRealm.management, LogKind.change, 'Registration',
                     f'Registration form "{form.title}" modified', user, data={'Changes': log_data})
        return _form_result(form, management=True)

    @registry.tool(read_only=False)
    def indico_clone_registration_form(user, event_id: ID, form_id: ID, title: TITLE):
        """Clone a registration form and its fields without copying registrations."""
        event = _event(event_id)
        _manager(user, event, write=True)
        if not title.strip():
            raise BadRequest('The title cannot be blank.')
        source = _form(event, form_id)
        form = clone_registration_form(source, title.strip())
        form.log(EventLogRealm.management, LogKind.positive, 'Registration',
                 f'Registration form cloned from "{source.title}"', user, data={'Source form ID': source.id})
        return _form_result(form, management=True)

    @registry.tool(read_only=False, idempotent=True)
    def indico_schedule_registration_form(user, event_id: ID, form_id: ID, start: str,
                                          end: str | None = None, modification_end: str | None = None):
        """Set a registration form's start, closing and modification times using ISO 8601 timestamps."""
        event = _event(event_id)
        _manager(user, event, write=True)
        form = _form(event, form_id, lock=True)
        start_dt, end_dt, mod_dt = _datetime(start), _datetime(end), _datetime(modification_end)
        if (end_dt and end_dt <= start_dt) or (mod_dt and mod_dt < start_dt):
            raise BadRequest('Closing and modification times must follow the opening time.')
        form.start_dt, form.end_dt, form.modification_end_dt = start_dt, end_dt, mod_dt
        form.log(EventLogRealm.management, LogKind.change, 'Registration',
                 f'Registration form "{form.title}" scheduled', user)
        return _form_result(form, management=True)

    @registry.tool(read_only=False, destructive=True, idempotent=True)
    def indico_set_registration_form_open(user, event_id: ID, form_id: ID, open: bool, confirm: bool):
        """Open or close a form immediately using the event's registration management permission."""
        _confirmed(confirm)
        event = _event(event_id)
        _manager(user, event, write=True)
        form = _form(event, form_id, lock=True)
        if open != form.is_open:
            if open:
                form.start_dt = min(form.start_dt, now_utc()) if form.start_dt else now_utc()
                if form.has_ended:
                    form.end_dt = None
            else:
                close_registration(form)
            status = 'opened' if open else 'closed'
            form.log(EventLogRealm.event, LogKind.change, 'Registration',
                     f'Registration form "{form.title}" was {status}', user)
        return _form_result(form, management=True)

    @registry.tool(read_only=False, destructive=True)
    def indico_delete_registration_form(user, event_id: ID, form_id: ID, confirm: bool):
        """Soft-delete a registration form and remove its references from access lists."""
        _confirmed(confirm)
        event = _event(event_id)
        _manager(user, event, write=True)
        form = _form(event, form_id, lock=True)
        for relation in ('in_attachment_acls', 'in_attachment_folder_acls', 'in_contribution_acls', 'in_event_acls',
                         'in_session_acls'):
            getattr(form, relation).delete()
        form.is_deleted = True
        signals.event.registration_form_deleted.send(form)
        form.log(EventLogRealm.management, LogKind.negative, 'Registration',
                 f'Registration form "{form.title}" was deleted', user)
        return {'id': form.id, 'deleted': True}

    @registry.tool(idempotent=True)
    def indico_list_registration_field_types(user, event_id: ID):
        """List field types available to registration managers, including optional plugin fields."""
        _manager(user, _event(event_id))
        return {'types': sorted(get_field_types())}

    @registry.tool(read_only=False)
    def indico_create_registration_section(user, event_id: ID, form_id: ID, title: TITLE,
                                           description: Annotated[str, Field(max_length=10000)] = '',
                                           manager_only: bool = False):
        """Add a section for participant fields or manager-only fields to a registration form."""
        event = _event(event_id)
        _manager(user, event, write=True)
        form = _form(event, form_id, lock=True)
        if form.is_purged or not title.strip():
            raise BadRequest('An active form and a nonblank title are required.')
        section = RegistrationFormSection(registration_form=form, title=title.strip(), description=description,
                                          is_manager_only=manager_only)
        db.session.add(section)
        db.session.flush()
        section.log(EventLogRealm.management, LogKind.positive, 'Registration',
                    f'Section "{section.title}" in "{form.title}" added', user, data={'Manager-only': manager_only})
        return {'id': section.id, 'section': section.view_data}

    @registry.tool(read_only=False)
    def indico_add_registration_field(user, event_id: ID, form_id: ID, section_id: ID, input_type: TITLE, title: TITLE,
                                      description: Annotated[str, Field(max_length=10000)] = '',
                                      required: bool = False, options: FORM_DATA | None = None):
        """Add a field to a section. Supply type-specific options as accepted by the Indico form editor."""
        event = _event(event_id)
        _manager(user, event, write=True)
        form = _form(event, form_id, lock=True)
        section = next((section for section in form.active_sections if section.id == section_id), None)
        if section is None:
            raise NotFound
        if form.is_purged:
            raise Forbidden
        field = RegistrationFormField(parent=section, registration_form=form)
        _save_field(field, input_type, title, description, required, options)
        db.session.add(field)
        db.session.flush()
        signals.event.registration_form_field_added.send(field)
        field.log(EventLogRealm.management, LogKind.positive, 'Registration',
                  f'Field "{field.title}" in "{form.title}" added', user, data={'Type': input_type})
        return {'id': field.id, 'name': field.html_field_name, 'field': field.view_data}

    @registry.tool(read_only=False, destructive=True)
    def indico_delete_registration_field(user, event_id: ID, form_id: ID, field_id: ID, confirm: bool):
        """Remove a custom registration field that is unused as a condition for other fields."""
        _confirmed(confirm)
        event = _event(event_id)
        _manager(user, event, write=True)
        form = _form(event, form_id, lock=True)
        field = next((item for item in form.form_items if item.id == field_id and item.is_field
                      and not item.is_deleted and not item.parent.is_deleted), None)
        if field is None:
            raise NotFound
        if field.type == RegistrationFormItemType.field_pd or field.condition_for:
            raise BadRequest('A personal data field or a conditional dependency cannot be removed.')
        signals.event.registration_form_field_deleted.send(field)
        field.is_deleted = True
        field.show_if_id = field.show_if_values = None
        update_regform_item_positions(form)
        db.session.flush()
        field.log(EventLogRealm.management, LogKind.negative, 'Registration',
                  f'Field "{field.title}" in "{form.title}" deleted', user)
        return {'id': field.id, 'deleted': True}
