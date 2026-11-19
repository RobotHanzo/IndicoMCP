"""MCP tools for the optional STSA registration plugin."""

from decimal import Decimal
from hashlib import sha256
from json import dumps
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BeforeValidator, Field, PositiveInt
from werkzeug.datastructures import MultiDict
from werkzeug.exceptions import BadRequest, Forbidden, NotFound

_GLOBAL_SETTINGS = (
    'rewrite_email_subjects', 'email_subject_prefix', 'wallet_badges', 'wallet_pass_design',
    'cjk_badge_fonts', 'payment_reminders', 'lock_member_email', 'autofill_member_profile',
)
_APPROVAL_AGE = 600


def _require_true(value):
    if value is not True:
        raise ValueError('Confirmation must be true.')
    return value


Confirmed = Annotated[Literal[True], BeforeValidator(_require_true)]


def _form_data(values):
    return MultiDict((key, str(value).lower() if isinstance(value, bool) else str(value))
                     for key, value in values.items() if value is not None)


def _validate(form):
    if not form.validate():
        raise BadRequest(' '.join(f'{field}: {error}' for field, errors in form.errors.items() for error in errors))


def _require_admin(user):
    if not user.is_admin:
        raise Forbidden('Only Indico administrators may use this tool.')


def _managed_event(user, event_id, *, write=False):
    from indico.modules.events.models.events import Event

    event = Event.get(event_id, is_deleted=False)
    if event is None:
        raise NotFound('The event does not exist.')
    if not event.can_manage(user, permission='registration'):
        raise Forbidden('You do not have registration management permission for this event.')
    if write and event.is_locked:
        raise Forbidden('The event is locked.')
    return event


def _managed_regform(user, event_id, registration_form_id, *, write=False):
    from indico.modules.events.registration.models.forms import RegistrationForm

    event = _managed_event(user, event_id, write=write)
    query = RegistrationForm.query.filter_by(event_id=event.id, id=registration_form_id, is_deleted=False)
    if write:
        query = query.enable_eagerloads(False).with_for_update(of=RegistrationForm)
    regform = query.one_or_none()
    if regform is None:
        raise NotFound('The registration form does not exist in this event.')
    return regform


def _ticket_category(user, category_id, *, write=False):
    from indico.modules.categories import Category

    _require_admin(user)
    query = Category.query.filter_by(id=category_id, is_deleted=False)
    if write:
        query = query.enable_eagerloads(False).with_for_update(of=Category)
    category = query.one_or_none()
    if category is None:
        raise NotFound('The ticket category does not exist.')
    return category


def _regform_settings(regform, settings):
    from indico_stsa.constants import APPLIES_TO_BASE, PERCENT
    from indico_stsa.util import is_in_events_api

    return {
        'member_discount_enabled': settings.member_discount_enabled if settings else False,
        'discount_type': settings.discount_type if settings else PERCENT,
        'discount_value': str(settings.discount_value) if settings else '0',
        'applies_to': settings.applies_to if settings else APPLIES_TO_BASE,
        'notice_text': settings.notice_text if settings else '',
        'group_login_required': settings.group_login_required if settings else False,
        'include_in_events_api': is_in_events_api(regform),
    }


def _editable_settings(regform):
    from indico.core.db import db
    from indico_stsa.models.settings import STSASettings
    from indico_stsa.util import get_settings, tables_exist

    if not tables_exist():
        raise BadRequest('The STSA database migrations must be applied before changing registration settings.')
    settings = get_settings(regform)
    if settings is None:
        settings = STSASettings(registration_form=regform)
        db.session.add(settings)
    return settings


def _reminder_form(regform, registrations, sender_address, subject, body, copy_for_sender, user):
    from indico.modules.events.registration.forms import EmailRegistrantsForm
    from indico_stsa.reminders import default_body, default_subject

    with regform.event.force_event_locale():
        values = {
            'sender_address': user.email if sender_address is None else sender_address,
            'subject': default_subject() if subject is None else subject,
            'body': default_body() if body is None else body,
            'copy_for_sender': copy_for_sender,
        }
    form = EmailRegistrantsForm(formdata=_form_data(values), csrf_enabled=False, regform=regform,
                                registration_id=[r.id for r in registrations],
                                recipients=[r.email for r in registrations], cc_addresses=[])
    del form.attach_ticket
    _validate(form)
    return form


def _reminder_fingerprint(user, regform, registrations, form):
    snapshot = {
        'user': user.id,
        'form': regform.id,
        'recipients': sorted((r.id, r.email, str(r.price)) for r in registrations),
        'sender': form.sender_address.data,
        'subject': form.subject.data,
        'body': form.body.data,
        'copy_for_sender': form.copy_for_sender.data,
    }
    return sha256(dumps(snapshot, sort_keys=True).encode()).hexdigest()


def _approval_serializer():
    from flask import current_app
    from itsdangerous import URLSafeTimedSerializer

    return URLSafeTimedSerializer(current_app.secret_key, salt='indico-mcp-stsa-payment-reminder')


def _reminder_preview(form, registration):
    from indico.util.placeholders import replace_placeholders
    from indico.util.string import sanitize_html
    from indico.web.flask.templating import get_template_module

    def render(text):
        text = text.replace('{link}', '[registration link]').replace('{picture}', '[registration picture]')
        return replace_placeholders('registration-email', text, regform=form.regform, registration=registration)

    with form.regform.event.force_event_locale(registration.user):
        template = get_template_module('events/registration/emails/custom_email.html',
                                       email_subject=render(form.subject.data), email_body=render(form.body.data))
        return {'subject': str(template.get_subject()), 'body_html': sanitize_html(str(template.get_body()))}


def register_tools(registry, active_plugins):
    plugin = active_plugins.get('stsa')
    if plugin is None:
        return

    import pytz
    from indico.core import signals
    from indico.core.config import config
    from indico.core.db import db
    from indico.core.plugins import url_for_plugin
    from indico.modules.events.registration.controllers.management.fields import _fill_form_field_with_data
    from indico.modules.events.registration.models.form_fields import RegistrationFormField
    from indico.modules.events.registration.models.forms import RegistrationForm
    from indico.modules.events.registration.models.items import PersonalDataType, RegistrationFormSection
    from indico.modules.events.registration.models.registrations import Registration
    from indico.modules.logs import EventLogRealm, LogKind
    from indico.modules.logs.models.entries import EventLogEntry
    from indico_stsa.attendance import parse_statuses, parse_window
    from indico_stsa.constants import PROFILE_FIELDS
    from indico_stsa.controllers.management import RHSTSAPaymentReminders
    from indico_stsa.default_names import _move_to_the_end
    from indico_stsa.forms import RegFormSettingsForm
    from indico_stsa.payments import find_unpaid, format_outstanding
    from indico_stsa.pricing import apply_member_discount, is_member_discounted
    from indico_stsa.registered_events import describe, find_registered
    from indico_stsa.util import (
        NAME_COLUMNS,
        default_name_state,
        get_discount_data,
        get_settings,
        is_group_plugin_installed,
        provision_discount_field,
        tables_exist,
    )
    from itsdangerous import BadData
    from marshmallow import ValidationError

    profile_types = {claim: field_type for field_type, claim in PROFILE_FIELDS.items()}

    @registry.tool()
    def indico_stsa_registered_events(
        user, from_date: Annotated[str, Field(description='ISO 8601 start date or date-time')] | None = None,
        to_date: Annotated[str, Field(description='ISO 8601 end date or date-time')] | None = None,
        statuses: list[Literal['upcoming', 'ongoing', 'checked_in', 'no_show', 'pending', 'withdrawn', 'rejected']]
        | None = None,
    ):
        """List your registrations and attendance for forms included in the STSA events API."""
        try:
            filters = parse_statuses(statuses or [])
            window = parse_window(from_date, to_date, tz=pytz.timezone(config.DEFAULT_TIMEZONE))
        except ValueError as exc:
            raise BadRequest(str(exc)) from exc
        return {'registrations': [describe(registration, status)
                                  for registration, status in find_registered(user, statuses=filters, window=window)]}

    def describe_form(regform):
        return {
            'id': regform.id,
            'title': regform.title,
            'currency': regform.currency,
            'settings': _regform_settings(regform, get_settings(regform)),
            'default_names_disabled': {name: default_name_state(regform, name) for name in NAME_COLUMNS},
            'discounted_registrations': sum(not registration.is_deleted and is_member_discounted(registration)
                                            for registration in regform.registrations),
            'participant_sections': [
                {'id': section.id, 'title': section.title} for section in regform.sections
                if section.is_enabled and not section.is_deleted and not section.is_manager_only
            ],
            'profile_fields': [
                {'id': field.id, 'title': field.title, 'claim': PROFILE_FIELDS[field.input_type],
                 'section_id': field.parent_id, 'enabled': field.is_active}
                for field in regform.form_items
                if field.is_field and not field.is_deleted and field.input_type in PROFILE_FIELDS
            ],
        }

    @registry.tool()
    def indico_stsa_registration_settings(user, event_id: PositiveInt, registration_form_id: PositiveInt | None = None):
        """Read STSA options, name-field switches and discount counts for one form or all forms of an event."""
        event = _managed_event(user, event_id)
        forms = RegistrationForm.query.filter_by(event_id=event.id, is_deleted=False)
        if registration_form_id is not None:
            forms = forms.filter_by(id=registration_form_id)
        forms = forms.order_by(RegistrationForm.title, RegistrationForm.id).all()
        if not forms and registration_form_id is not None:
            raise NotFound('The registration form does not exist in this event.')
        return {'event_id': event.id, 'group_registration_active': is_group_plugin_installed(),
                'tables_ready': tables_exist(), 'forms': [describe_form(regform) for regform in forms]}

    @registry.tool(read_only=False, idempotent=True)
    def indico_stsa_update_registration_settings(
        user, event_id: PositiveInt, registration_form_id: PositiveInt,
        member_discount_enabled: bool | None = None,
        discount_type: Literal['percent', 'amount'] | None = None,
        discount_value: Annotated[str, Field(pattern=r'^\d{1,9}(\.\d{1,2})?$',
                                             description='Nonnegative decimal such as 12.50')] | None = None,
        applies_to: Literal['base', 'total'] | None = None, notice_text: str | None = None,
        group_login_required: bool | None = None, include_in_events_api: bool | None = None,
    ):
        """Update STSA settings for a form. Applying new discounts to existing registrations is a separate tool."""
        requested = {
            'member_discount_enabled': member_discount_enabled,
            'discount_type': discount_type,
            'discount_value': discount_value,
            'applies_to': applies_to,
            'notice_text': notice_text,
            'group_login_required': group_login_required,
            'include_in_events_api': include_in_events_api,
        }
        updates = {key: value for key, value in requested.items() if value is not None}
        regform = _managed_regform(user, event_id, registration_form_id, write=True)
        if not updates:
            return describe_form(regform)
        group_active = is_group_plugin_installed()
        if 'group_login_required' in updates and not group_active:
            raise BadRequest('The group registration plugin must be active to change the group login requirement.')

        values = _regform_settings(regform, get_settings(regform)) | updates
        form = RegFormSettingsForm(formdata=_form_data(values), csrf_enabled=False,
                                   group_plugin_installed=group_active)
        _validate(form)
        if form.member_discount_enabled.data and not form.discount_value.data:
            raise BadRequest('Set a positive discount value before enabling the member discount.')

        settings = _editable_settings(regform)
        for name in values:
            if name in form:
                value = form[name].data
                setattr(settings, name, value if value is not None else Decimal(0))
        if settings.member_discount_enabled:
            provision_discount_field(regform)
        db.session.flush()
        regform.log(EventLogRealm.management, LogKind.change, 'Registration', 'STSA settings updated', user)
        return describe_form(regform)

    @registry.tool(read_only=False)
    def indico_stsa_add_profile_field(
        user, event_id: PositiveInt, registration_form_id: PositiveInt, section_id: PositiveInt,
        claim: Literal['chinese_name', 'english_name', 'major', 'year_of_study'],
        title: Annotated[str, Field(min_length=1)], description: str = '', required: bool = False,
    ):
        """Add a field that the participant's signed-in STSA account can fill automatically."""
        regform = _managed_regform(user, event_id, registration_form_id, write=True)
        section = RegistrationFormSection.query.filter_by(
            id=section_id, registration_form_id=regform.id, is_enabled=True, is_deleted=False, is_manager_only=False,
        ).one_or_none()
        if section is None:
            raise NotFound('The active participant section does not exist on this form.')
        field = RegistrationFormField(parent=section, registration_form=regform)
        with plugin.plugin_context():
            try:
                _fill_form_field_with_data(field, {'input_type': profile_types[claim], 'title': title,
                                                  'description': description, 'is_required': required})
            except ValidationError as exc:
                raise BadRequest(str(exc)) from exc
            db.session.add(field)
            db.session.flush()
            signals.event.registration_form_field_added.send(field)
        field.log(EventLogRealm.management, LogKind.positive, 'Registration',
                  f'Field "{field.title}" in "{regform.title}" added', user, data={'Type': field.input_type})
        return {'registration_form_id': regform.id, 'section_id': section.id, 'field_id': field.id, 'claim': claim}

    @registry.tool(read_only=False, destructive=True, idempotent=True)
    def indico_stsa_recalculate_discounts(user, event_id: PositiveInt, registration_form_id: PositiveInt,
                                        confirm: Confirmed):
        """Reprice existing registrations, including paid registrations. A disabled discount is removed."""
        regform = _managed_regform(user, event_id, registration_form_id, write=True)
        if not tables_exist():
            raise BadRequest('The STSA database migrations must be applied before recalculating discounts.')
        registrations = Registration.query.with_parent(regform).filter_by(is_deleted=False).all()

        def discount_snapshot(registration):
            data = get_discount_data(registration)
            return dict(data.data) if data and data.data else {}

        changed = 0
        for registration in registrations:
            before = discount_snapshot(registration)
            apply_member_discount(registration, management=True)
            changed += before != discount_snapshot(registration)
        if changed:
            regform.log(EventLogRealm.management, LogKind.change, 'Registration',
                        'STSA member discounts recalculated', user)
        return {'registration_form_id': regform.id, 'evaluated': len(registrations), 'changed': changed}

    @registry.tool(read_only=False, idempotent=True)
    def indico_stsa_set_default_name_field(
        user, event_id: PositiveInt, registration_form_id: PositiveInt, name: Literal['first_name', 'last_name'],
        enabled: bool,
    ):
        """Switch a default name field when an enabled STSA profile field can supply the name."""
        regform = _managed_regform(user, event_id, registration_form_id, write=True)
        disabled = default_name_state(regform, name)
        if disabled is None:
            raise BadRequest('This default name field requires an active, unconditional STSA replacement field.')
        field = regform.get_personal_data_field(PersonalDataType[name])
        if field is None:
            raise NotFound('The default name field was not found.')
        if disabled != (not enabled):
            settings = _editable_settings(regform)
            setattr(settings, NAME_COLUMNS[name], not enabled)
            if not enabled:
                _move_to_the_end(field)
            db.session.flush()
            signals.event.registration_form_field_toggled.send(field)
            field.log(EventLogRealm.management, LogKind.positive if enabled else LogKind.negative, 'Registration',
                      f'Field "{field.title}" in "{regform.title}" {"enabled" if enabled else "disabled"}', user)
        return {'registration_form_id': regform.id, 'field_id': field.id, 'enabled': enabled}

    def require_payment_reminders(regform):
        if not plugin.settings.get('payment_reminders') or not regform.event.has_feature('payment'):
            raise NotFound('STSA payment reminders are unavailable for this event.')

    @registry.tool()
    def indico_stsa_preview_payment_reminders(
        user, event_id: PositiveInt, registration_form_id: PositiveInt, sender_address: str | None = None,
        subject: str | None = None, body: str | None = None, copy_for_sender: bool = False,
    ):
        """Preview a payment reminder and obtain a short-lived token authorizing this message and recipient set."""
        regform = _managed_regform(user, event_id, registration_form_id)
        require_payment_reminders(regform)
        registrations = find_unpaid(regform)
        if not registrations:
            return {'registration_form_id': regform.id, 'recipient_count': 0}
        with plugin.plugin_context():
            form = _reminder_form(regform, registrations, sender_address, subject, body, copy_for_sender, user)
            fingerprint = _reminder_fingerprint(user, regform, registrations, form)
            return {
                'registration_form_id': regform.id,
                'recipient_count': len(registrations),
                'outstanding': format_outstanding(registrations, regform.currency),
                'sender_address': form.sender_address.data,
                'allowed_senders': dict(form.sender_address.choices),
                'subject': form.subject.data,
                'body': form.body.data,
                'example': _reminder_preview(form, registrations[0]),
                'approval_token': _approval_serializer().dumps({'fingerprint': fingerprint, 'nonce': uuid4().hex}),
                'approval_expires_in_seconds': _APPROVAL_AGE,
            }

    @registry.tool(read_only=False, destructive=True, idempotent=True, open_world=True)
    def indico_stsa_send_payment_reminders(
        user, event_id: PositiveInt, registration_form_id: PositiveInt,
        approval_token: Annotated[str, Field(min_length=1, max_length=512)], confirm: Confirmed,
        sender_address: str | None = None, subject: str | None = None, body: str | None = None,
        copy_for_sender: bool = False,
    ):
        """Send the previewed message to all registrants who still owe payment. Reuse the preview's message inputs."""
        regform = _managed_regform(user, event_id, registration_form_id, write=True)
        event = regform.event
        marker = {'stsa_mcp_reminder': sha256(approval_token.encode()).hexdigest(), 'registration_form_id': regform.id}
        previous = (EventLogEntry.query.filter_by(event_id=event.id, user_id=user.id)
                    .filter(EventLogEntry.meta.contains(marker)).first())
        if previous is not None:
            return {'registration_form_id': regform.id, 'queued': previous.data['Recipient count']}
        require_payment_reminders(regform)
        try:
            approval = _approval_serializer().loads(approval_token, max_age=_APPROVAL_AGE)
        except BadData as exc:
            raise BadRequest('Preview the payment reminder again to obtain a new approval token.') from exc

        registrations = find_unpaid(regform)
        with plugin.plugin_context():
            form = _reminder_form(regform, registrations, sender_address, subject, body, copy_for_sender, user)
            if _reminder_fingerprint(user, regform, registrations, form) != approval['fingerprint']:
                raise BadRequest('The reminder or outstanding registrations changed. Preview the reminder again.')
            handler = RHSTSAPaymentReminders()
            handler.event = event
            handler.regform = regform
            handler.registrations = registrations
            handler._send_emails(form)
        event.log(EventLogRealm.management, LogKind.other, 'Registration', 'STSA payment reminders queued via MCP',
                  user, data={'Recipient count': len(registrations)}, meta=marker)
        return {'registration_form_id': regform.id, 'queued': len(registrations)}

    @registry.tool()
    def indico_stsa_site_settings(user):
        """Read the STSA site-wide switches and links to the administrator's API reference."""
        _require_admin(user)
        return {
            'settings': {key: plugin.settings.get(key) for key in _GLOBAL_SETTINGS},
            'api_reference_url': url_for_plugin('stsa.api_docs', _external=True),
            'openapi_url': url_for_plugin('stsa.api_docs_spec', _external=True),
        }

    @registry.tool(read_only=False, idempotent=True)
    def indico_stsa_update_site_settings(
        user, rewrite_email_subjects: bool | None = None, email_subject_prefix: str | None = None,
        wallet_badges: bool | None = None, wallet_pass_design: bool | None = None,
        cjk_badge_fonts: bool | None = None, payment_reminders: bool | None = None,
        lock_member_email: bool | None = None, autofill_member_profile: bool | None = None,
    ):
        """Update STSA switches for the entire Indico instance. Requires an Indico administrator."""
        _require_admin(user)
        inputs = locals()
        updates = {key: inputs[key] for key in _GLOBAL_SETTINGS if inputs[key] is not None}
        if updates:
            values = {key: plugin.settings.get(key) for key in _GLOBAL_SETTINGS} | updates
            with plugin.plugin_context():
                form = plugin.settings_form(formdata=_form_data(values), csrf_enabled=False)
                _validate(form)
                plugin.settings.set_multi({key: form[key].data for key in updates})
        return indico_stsa_site_settings(user)

    @registry.tool()
    def indico_stsa_openapi(user):
        """Read the live OpenAPI document from the STSA administrator API reference."""
        from indico_stsa.api_docs import generate_spec

        _require_admin(user)
        with plugin.plugin_context():
            return generate_spec()

    @registry.tool()
    def indico_stsa_ticket_status(user, category_id: Annotated[int, Field(ge=0)]):
        """Preview whether installation will create or refresh the packaged STSA ticket in a category."""
        from indico_stsa.install_ticket import BACKGROUND, TEMPLATE_TITLE, find_template

        category = _ticket_category(user, category_id)
        template = find_template(category)
        return {
            'category_id': category.id,
            'title': TEMPLATE_TITLE,
            'would_create': template is None,
            'template_id': template.id if template is not None else None,
            'current_default_template_id': category.default_ticket_template_id,
            'artwork_ready': BACKGROUND.is_file(),
        }

    @registry.tool(read_only=False, destructive=True, idempotent=True)
    def indico_stsa_install_ticket(
        user, category_id: Annotated[int, Field(ge=0)], set_as_default: bool, confirm: Confirmed,
    ):
        """Install or refresh the packaged STSA ticket design in a category, optionally setting its default ticket."""
        from indico_stsa.install_ticket import install

        category = _ticket_category(user, category_id, write=True)
        with plugin.plugin_context():
            template, created = install(category, set_as_default=set_as_default)
        return {'category_id': category.id, 'template_id': template.id, 'title': template.title,
                'created': created, 'is_default': category.default_ticket_template_id == template.id}
