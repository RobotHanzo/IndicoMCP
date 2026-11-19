"""MCP tools for the optional Group Registration and Sentry plugins."""

from datetime import datetime, timezone
from typing import Annotated, Literal

from indico.core.db import db
from indico.core.plugins import url_for_plugin
from indico.modules.events import Event
from indico.modules.events.registration.models.forms import RegistrationForm
from indico.modules.events.registration.models.registrations import Registration
from indico.modules.logs import EventLogRealm, LogKind
from indico.web.flask.util import url_for
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import joinedload
from werkzeug.datastructures import MultiDict
from werkzeug.exceptions import BadRequest, Forbidden, NotFound

PositiveId = Annotated[int, Field(gt=0, le=2_147_483_647)]
PageSize = Annotated[int, Field(ge=1, le=100)]
Offset = Annotated[int, Field(ge=0, le=2_147_483_647)]
Confirmation = Annotated[bool, Field(description='Set to true to confirm this action.')]


class GroupPlanInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    id: Annotated[str, Field(min_length=1, max_length=100)] | None = None
    label: Annotated[str, Field(min_length=1, max_length=200)]
    size: Annotated[int, Field(ge=1)]
    type: Literal['percent', 'amount'] | None = None
    value: Annotated[float, Field(ge=0, allow_inf_nan=False)] = 0


class SentrySettingsUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    environment: str = ''
    release: str = ''
    enable_logging: bool = True
    logging_level: Literal['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'] = 'WARNING'
    enable_celery: bool = True
    enable_sqlalchemy: bool = False
    enable_redis: bool = True
    attach_stacktrace: bool = True
    max_breadcrumbs: Annotated[int, Field(ge=0, le=500)] = 100
    traces_sample_rate: Annotated[float, Field(ge=0, le=1)] = 0
    profiles_sample_rate: Annotated[float, Field(ge=0, le=1)] = 0
    sample_rate_overrides: list[str] = Field(default_factory=list, max_length=100)
    ignored_exceptions: list[str] = Field(default_factory=list, max_length=100)
    ignored_loggers: list[str] = Field(default_factory=list, max_length=100)
    ignored_transactions: list[str] = Field(default_factory=list, max_length=100)
    event_context: bool = True
    category_context: bool = True
    frontend_environment: str = ''
    frontend_release: str = ''
    frontend_traces_sample_rate: Annotated[float, Field(ge=0, le=1)] = 0
    frontend_ignored_errors: list[str] = Field(default_factory=list, max_length=100)
    frontend_denied_urls: list[str] = Field(default_factory=list, max_length=100)


def register_tools(registry, active_plugins):
    if plugin := active_plugins.get('group_registration'):
        _register_group_tools(registry, plugin)
    if plugin := active_plugins.get('sentry'):
        _register_sentry_tools(registry, plugin)


def _get_event(event_id):
    event = Event.get(event_id)
    if event is None or event.is_deleted:
        raise NotFound('Event not found.')
    return event


def _get_form(event_id, reg_form_id):
    event = _get_event(event_id)
    if not event.has_feature('registration'):
        raise NotFound('Registration is disabled for this event.')
    regform = RegistrationForm.query.filter_by(id=reg_form_id, event_id=event.id, is_deleted=False).one_or_none()
    if regform is None:
        raise NotFound('Registration form not found in this event.')
    return regform


def _check_manager(event, user, *, write=False):
    if not event.can_manage(user, permission='registration'):
        raise Forbidden('Registration management permission is required.')
    if write and event.is_locked:
        raise Forbidden('This event is locked.')


def _manager_form(user, event_id, reg_form_id, *, write=False):
    regform = _get_form(event_id, reg_form_id)
    _check_manager(regform.event, user, write=write)
    return regform


def _check_participant_form(regform, user, *, form_token=None, allow_restricted=False, registration=None):
    event = regform.event
    if not event.can_access(user):
        if (not allow_restricted or event.access_key or not event.public_regform_access
                or not regform.in_event_acls.filter_by(event_id=event.id).has_rows()):
            raise Forbidden('You do not have access to this registration form.')
    if regform.private and registration is None and (not form_token or regform.uuid != form_token):
        raise Forbidden('This registration form is private.')


def _participant_form(user, event_id, reg_form_id, form_token):
    regform = _get_form(event_id, reg_form_id)
    _check_participant_form(regform, user, form_token=form_token, allow_restricted=True)
    return regform


def _check_admin(user):
    if not user.is_admin:
        raise Forbidden('Indico administrator permission is required.')


def _check_confirmation(confirm):
    if confirm is not True:
        raise BadRequest('Explicit confirmation is required.')


def _parse_datetime(value):
    if value is None:
        return None
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            raise ValueError('Missing timezone')
        return result.astimezone(timezone.utc)
    except (ValueError, OverflowError) as exc:
        raise BadRequest('Use an ISO 8601 date and time with a timezone offset.') from exc


def _iso(value):
    return value.isoformat() if value is not None else None


def _registration_display_url(regform, *, form_token=None, group_code=None):
    return url_for('event_registration.display_regform', regform, form_token=form_token,
                   group_code=group_code, _external=True)


def _log(event, user, kind, message, **kwargs):
    event.log(EventLogRealm.management, kind, 'Registration', message, user, **kwargs)


def _register_group_tools(registry, plugin):
    from indico.modules.events.registration.controllers.management.reglists import RHRegistrationEmailRegistrants
    from indico.modules.events.registration.forms import EmailRegistrantsForm
    from indico.modules.events.registration.notifications import notify_registration_state_update
    from indico.util.placeholders import replace_placeholders
    from indico.web.flask.templating import get_template_module
    from indico_group_registration.constants import MODE_CREATE, MODE_JOIN, PLAN_FIELD
    from indico_group_registration.controllers.management import DEFAULT_DISCLAIMER, _forming_group_members
    from indico_group_registration.forms import GroupSettingsForm, _new_plan_id
    from indico_group_registration.models.groups import GroupState, RegistrationGroup
    from indico_group_registration.models.members import GroupMember
    from indico_group_registration.models.settings import GroupSettings
    from indico_group_registration.notifications import notify_group_dissolved
    from indico_group_registration.operations import (
        GroupError,
        dissolve_group,
        leave_group,
        recount_group,
        regenerate_join_credentials,
        switch_plan,
    )
    from indico_group_registration.plans import get_plan
    from indico_group_registration.pricing import pricing_in_progress, record_balance_payment, sync_balance_state
    from indico_group_registration.reconcile import reconcile_group
    from indico_group_registration.reminders import default_body, default_subject
    from indico_group_registration.util import (
        find_field,
        find_group_by_uuid,
        get_group_settings,
        get_plans,
        get_switchable_plans,
        provision_fields,
        resolve_join_target,
    )

    def group_query(regform):
        return RegistrationGroup.query.filter_by(registration_form_id=regform.id)

    def group_record(regform, group_id, *, lock=False):
        query = group_query(regform).filter_by(id=group_id)
        if lock:
            query = query.populate_existing().enable_eagerloads(False).with_for_update(of=RegistrationGroup)
        group = query.one_or_none()
        if group is None:
            raise NotFound('Group not found in this registration form.')
        return group

    def group_summary(group, *, include_code=False):
        return {
            'id': group.id, 'name': group.name, 'state': group.state.name, 'plan_id': group.plan_id,
            'effective_plan_id': group.pricing_plan.id if group.pricing_plan else None,
            'members': group.member_count, 'target': group.target_size, 'seats_left': group.seats_left,
            'created_at': _iso(group.created_dt), 'leader_registration_id': group.leader_registration_id,
            **({'code': group.formatted_code} if include_code else {}),
        }

    def group_members(group, *, include_ids=False):
        return [
            {'name': member.registration.full_name, 'state': member.registration.state.name,
             'leader': member.registration_id == group.leader_registration_id,
             **({'registration_id': member.registration_id} if include_ids else {})}
            for member in group.members if member.registration is not None and not member.registration.is_deleted
        ]

    def settings_data(regform):
        settings = get_group_settings(regform)
        if settings is None:
            return dict(enabled=False, plans=[], applies_to='base', reconciliation_dt=None,
                        allow_early_payment=True, max_groups_per_user=1, count_pending=True,
                        revoke_on_member_loss=False, disclaimer_text=str(DEFAULT_DISCLAIMER))
        return {key: getattr(settings, key) for key in (
            'enabled', 'plans', 'applies_to', 'reconciliation_dt', 'allow_early_payment',
            'max_groups_per_user', 'count_pending', 'revoke_on_member_loss', 'disclaimer_text',
        )}

    def settings_summary(regform):
        settings = get_group_settings(regform)
        data = settings_data(regform)
        return {**data, 'reconciliation_dt': _iso(data['reconciliation_dt']),
                'disclaimer_version': settings.disclaimer_version if settings else None,
                'effective_reconciliation_dt': _iso(settings.get_reconciliation_dt() if settings else
                                                     regform.end_dt or regform.event.start_dt)}

    def own_group(user, event_id, reg_form_id, *, leader=False, write=False):
        regform = _get_form(event_id, reg_form_id)
        registration = regform.get_registration(user=user)
        _check_participant_form(regform, user, registration=registration)
        if registration is None:
            raise Forbidden('Your own registration is required.')
        settings = get_group_settings(regform)
        membership = registration.group_membership
        if settings is None or not settings.enabled or membership is None:
            raise NotFound('You do not have a group on this form.')
        if write:
            group = group_record(regform, membership.group_id, lock=True)
            db.session.refresh(registration)
            db.session.expire(registration, ['group_membership'])
            membership = registration.group_membership
            if membership is None or membership.group_id != group.id or registration.is_deleted:
                raise NotFound('You do not have a group on this form.')
        else:
            group = membership.group
        if leader and (group.leader_registration_id != registration.id or not registration.is_active):
            raise Forbidden('An active group leader registration is required.')
        return registration, membership, group

    def credentials(group):
        return {'join_code': group.formatted_code,
                'join_url': url_for_plugin('group_registration.join_link', group.registration_form,
                                          join_uuid=str(group.join_uuid), _external=True)}

    def balance_members(regform):
        return (GroupMember.query.join(GroupMember.group).filter(RegistrationGroup.registration_form_id == regform.id)
                .options(joinedload(GroupMember.registration).joinedload(Registration.transaction),
                         joinedload(GroupMember.group)))

    @registry.tool()
    def indico_group_registration_form(
        user, event_id: PositiveId, reg_form_id: PositiveId, form_token: str | None = None,
    ):
        """Get available plans and the disclaimer before creating or joining a group through native registration."""
        regform = _get_form(event_id, reg_form_id)
        registration = regform.get_registration(user=user)
        _check_participant_form(regform, user, form_token=form_token, allow_restricted=True, registration=registration)
        settings = get_group_settings(regform)
        field = find_field(regform, PLAN_FIELD)
        field_is_visible = field in regform.active_fields and not field.parent.is_manager_only
        field_name = field.html_field_name if field_is_visible else None
        return {
            'enabled': bool(settings and settings.enabled and field_name), 'group_plan_field': field_name,
            'plans': [plan.serialize() for plan in get_plans(regform)] if settings and settings.enabled else [],
            'disclaimer': settings.disclaimer_text if settings and settings.enabled else '',
            'disclaimer_version': settings.disclaimer_version if settings and settings.enabled else None,
            'group_choice_examples': {
                'create': {'mode': MODE_CREATE, 'plan': '<plan id>', 'name': '<group name>', 'accepted': True},
                'join': {'mode': MODE_JOIN, 'code': '<group code>', 'accepted': True},
            } if settings and settings.enabled and field_name else {},
            'display_url': _registration_display_url(regform, form_token=form_token),
        }

    @registry.tool()
    def indico_group_registration_check_code(user, event_id: PositiveId, reg_form_id: PositiveId, code: str,
                                              form_token: str | None = None):
        """Check a shared group code and get the current plan and seats available for joining."""
        regform = _participant_form(user, event_id, reg_form_id, form_token)
        settings = get_group_settings(regform)
        if settings is None or not settings.enabled:
            raise NotFound('Group registration is disabled on this form.')
        group, error = resolve_join_target(regform, code)
        if error:
            return {'valid': False, 'error': str(error)}
        plan = group.chosen_plan
        return {'valid': True, 'name': group.name, 'code': group.formatted_code, 'members': group.member_count,
                'target': group.target_size, 'seats_left': group.seats_left, 'plan': plan.serialize() if plan else None}

    @registry.tool()
    def indico_group_registration_resolve_join_link(user, event_id: PositiveId, reg_form_id: PositiveId,
                                                     join_id: str, form_token: str | None = None):
        """Resolve the UUID from a Group Registration join link into the code for the native registration form."""
        regform = _participant_form(user, event_id, reg_form_id, form_token)
        settings = get_group_settings(regform)
        group = find_group_by_uuid(regform, join_id) if settings and settings.enabled else None
        if group is None:
            raise NotFound('This join link is no longer valid.')
        if regform.get_registration(user=user) is not None:
            return {'already_registered': True, 'display_url': _registration_display_url(regform)}
        _, error = resolve_join_target(regform, group.code)
        if error:
            raise BadRequest(str(error))
        return {'already_registered': False, 'code': group.formatted_code,
                'display_url': _registration_display_url(regform, form_token=form_token, group_code=group.code)}

    @registry.tool()
    def indico_group_registration_my_group(user, event_id: PositiveId, reg_form_id: PositiveId):
        """Show your group, members' names and states, available plan changes and your own balance due."""
        registration, member, group = own_group(user, event_id, reg_form_id)
        is_leader = registration.is_active and group.leader_registration_id == registration.id
        summary = group_summary(group)
        del summary['leader_registration_id']
        return {**summary, 'group_members': group_members(group), 'is_leader': is_leader,
                'balance_due': str(member.balance_due), 'currency': registration.currency,
                'switchable_plans': [plan.serialize() for plan in get_switchable_plans(group)] if is_leader else [],
                **(credentials(group) if group.state == GroupState.forming else {})}

    @registry.tool(read_only=False, destructive=True, open_world=True)
    def indico_group_registration_leave(user, event_id: PositiveId, reg_form_id: PositiveId, confirm: Confirmation):
        """Leave your unpaid group registration. The standard rate will apply and the group's rate may change."""
        _check_confirmation(confirm)
        registration, _, group = own_group(user, event_id, reg_form_id, write=True)
        if registration.is_paid:
            raise BadRequest('Already paid. Please contact the organizers to leave the group.')
        with plugin.plugin_context():
            try:
                leave_group(registration)
            except GroupError as exc:
                raise BadRequest(str(exc)) from exc
        return {'left_group_id': group.id, 'registration_id': registration.id}

    @registry.tool(read_only=False, destructive=True)
    def indico_group_registration_regenerate_join_link(user, event_id: PositiveId, reg_form_id: PositiveId,
                                                        confirm: Confirmation):
        """Rotate the join code and link of a group you lead. Previously shared codes and links stop working."""
        _check_confirmation(confirm)
        _, _, group = own_group(user, event_id, reg_form_id, leader=True, write=True)
        if group.state != GroupState.forming:
            raise BadRequest('Only forming groups have shareable join codes.')
        with plugin.plugin_context():
            try:
                regenerate_join_credentials(group)
            except GroupError as exc:
                raise BadRequest(str(exc)) from exc
        return credentials(group)

    @registry.tool(read_only=False, destructive=True, open_world=True)
    def indico_group_registration_switch_plan(user, event_id: PositiveId, reg_form_id: PositiveId,
                                              plan_id: str, confirm: Confirmation):
        """Switch the plan of an unpaid, forming group that you lead. This reprices the group."""
        _check_confirmation(confirm)
        registration, _, group = own_group(user, event_id, reg_form_id, leader=True, write=True)
        plan = get_plan(get_plans(registration.registration_form), plan_id)
        if plan is None:
            raise BadRequest('Unknown group plan.')
        with plugin.plugin_context():
            try:
                switch_plan(group, plan)
            except GroupError as exc:
                raise BadRequest(str(exc)) from exc
        return group_summary(group)

    @registry.tool()
    def indico_group_registration_overview(user, event_id: PositiveId, limit: PageSize = 50, offset: Offset = 0):
        """List registration forms and group counts for organizers with registration management permission."""
        event = _get_event(event_id)
        _check_manager(event, user)
        forms = (RegistrationForm.query.filter_by(event_id=event.id, is_deleted=False)
                 .order_by(RegistrationForm.title, RegistrationForm.id).offset(offset).limit(limit + 1).all())
        group_counts = dict(db.session.query(RegistrationGroup.registration_form_id, db.func.count())
                            .filter(RegistrationGroup.registration_form_id.in_([form.id for form in forms[:limit]]),
                                    RegistrationGroup.state != GroupState.dissolved)
                            .group_by(RegistrationGroup.registration_form_id).all())
        return {'forms': [{'id': form.id, 'title': form.title,
                           'enabled': bool(get_group_settings(form) and get_group_settings(form).enabled),
                           'group_count': group_counts.get(form.id, 0)} for form in forms[:limit]],
                'next_offset': offset + limit if len(forms) > limit else None}

    @registry.tool()
    def indico_group_registration_settings(user, event_id: PositiveId, reg_form_id: PositiveId):
        """Read a registration form's group configuration and effective reconciliation deadline as an organizer."""
        return settings_summary(_manager_form(user, event_id, reg_form_id))

    @registry.tool(read_only=False, destructive=True)
    def indico_group_registration_update_settings(
        user, event_id: PositiveId, reg_form_id: PositiveId, confirm: Confirmation, *, enabled: bool | None = None,
        plans: list[GroupPlanInput] | None = None, applies_to: Literal['base', 'total'] | None = None,
        reconciliation_dt: Annotated[str, Field(description='ISO 8601 with offset, or empty to clear')] | None = None,
        allow_early_payment: bool | None = None,
        max_groups_per_user: Annotated[int, Field(ge=0, le=2_147_483_647)] | None = None,
        count_pending: bool | None = None, revoke_on_member_loss: bool | None = None,
        disclaimer_text: Annotated[str, Field(max_length=20000)] | None = None,
    ):
        """Configure a form's group registration. Omit fields to keep current values and reuse existing plan IDs."""
        _check_confirmation(confirm)
        regform = _manager_form(user, event_id, reg_form_id, write=True)
        (RegistrationForm.query.filter_by(id=regform.id).enable_eagerloads(False)
         .with_for_update(of=RegistrationForm).one())
        db.session.expire(regform, ['group_settings'])
        settings = get_group_settings(regform)
        current = settings_data(regform)
        updates = dict(enabled=enabled, applies_to=applies_to, allow_early_payment=allow_early_payment,
                       max_groups_per_user=max_groups_per_user, count_pending=count_pending,
                       revoke_on_member_loss=revoke_on_member_loss, disclaimer_text=disclaimer_text)
        current.update({key: value for key, value in updates.items() if value is not None})
        if plans is not None:
            current_ids = {plan.id for plan in get_plans(regform)}
            if any(plan.id is not None and plan.id not in current_ids for plan in plans):
                raise BadRequest('Only existing plan IDs may be specified. Omit the ID of any new plan.')
            current['plans'] = [plan.model_dump() | {'id': plan.id or _new_plan_id()} for plan in plans]
        if reconciliation_dt is not None:
            current['reconciliation_dt'] = _parse_datetime(reconciliation_dt or None)
        with plugin.plugin_context():
            form = GroupSettingsForm(formdata=None, data=current, event=regform.event, csrf_enabled=False)
            if not form.validate():
                raise BadRequest(_format_form_errors(form))
        labels = [plan.label for plan in form.parsed_plans]
        if len(set(labels)) != len(labels):
            raise BadRequest('Group plan labels must be unique.')
        if settings is None:
            settings = GroupSettings(registration_form=regform)
            db.session.add(settings)
        previous_disclaimer = settings.disclaimer_text
        for name in current:
            if name != 'plans':
                setattr(settings, name, form[name].data)
        settings.plans = [plan.serialize() for plan in form.parsed_plans]
        if settings.disclaimer_text != previous_disclaimer:
            settings.disclaimer_version = (settings.disclaimer_version or 0) + 1
        if settings.enabled:
            with plugin.plugin_context():
                provision_fields(regform)
        db.session.flush()
        _log(regform.event, user, LogKind.change, f'Group registration settings for "{regform.title}"')
        return settings_summary(regform)

    @registry.tool()
    def indico_group_registration_list(user, event_id: PositiveId, reg_form_id: PositiveId,
                                       limit: PageSize = 50, offset: Offset = 0):
        """List groups and their current prices' plan IDs and seat counts as a registration organizer."""
        regform = _manager_form(user, event_id, reg_form_id)
        members = joinedload(RegistrationGroup.members).joinedload(GroupMember.registration)
        groups = (group_query(regform).options(members)
                  .order_by(RegistrationGroup.created_dt.desc(), RegistrationGroup.id.desc())
                  .offset(offset).limit(limit + 1).all())
        return {'groups': [group_summary(group, include_code=True) for group in groups[:limit]],
                'next_offset': offset + limit if len(groups) > limit else None}

    @registry.tool()
    def indico_group_registration_get(user, event_id: PositiveId, reg_form_id: PositiveId, group_id: PositiveId):
        """Show one group's plan, status and members to an organizer with registration management permission."""
        group = group_record(_manager_form(user, event_id, reg_form_id), group_id)
        return {**group_summary(group, include_code=True), 'group_members': group_members(group, include_ids=True)}

    @registry.tool(read_only=False, destructive=True, idempotent=True, open_world=True)
    def indico_group_registration_dissolve(user, event_id: PositiveId, reg_form_id: PositiveId,
                                           group_id: PositiveId, confirm: Confirmation):
        """Dissolve a group, reprice members at the standard rate and notify active members by email."""
        _check_confirmation(confirm)
        regform = _manager_form(user, event_id, reg_form_id, write=True)
        group = group_record(regform, group_id, lock=True)
        if group.state == GroupState.dissolved:
            return {'group_id': group.id, 'changed': False}
        with plugin.plugin_context():
            dissolve_group(group)
            notify_group_dissolved(group)
        _log(regform.event, user, LogKind.negative, f'Dissolved group "{group.name}" ({group.code})')
        return {'group_id': group.id, 'changed': True}

    @registry.tool(read_only=False, destructive=True, idempotent=True, open_world=True)
    def indico_group_registration_reconcile(user, event_id: PositiveId, reg_form_id: PositiveId,
                                            group_id: PositiveId, confirm: Confirmation):
        """Reprice a forming group as if its deadline had arrived and send the usual member notifications."""
        _check_confirmation(confirm)
        regform = _manager_form(user, event_id, reg_form_id, write=True)
        group = group_record(regform, group_id, lock=True)
        with plugin.plugin_context():
            result = reconcile_group(group)
        if result is not None:
            _log(regform.event, user, LogKind.change, f'Reconciled group "{group.name}" ({group.code})')
        return {'group_id': group.id, 'state': group.state.name, 'changed': result is not None}

    @registry.tool(read_only=False, destructive=True, idempotent=True, open_world=True)
    def indico_group_registration_restore_short_groups(user, event_id: PositiveId, reg_form_id: PositiveId,
                                                       confirm: Confirmation):
        """Restore eligible short group rates after membership recovers and notify members whose rates changed."""
        _check_confirmation(confirm)
        regform = _manager_form(user, event_id, reg_form_id, write=True)
        groups = (group_query(regform).filter_by(state=GroupState.short).order_by(RegistrationGroup.id)
                  .enable_eagerloads(False)
                  .with_for_update(of=RegistrationGroup).all())
        restored = []
        with plugin.plugin_context():
            for group in groups:
                before = (group.state, group.effective_plan_id)
                recount_group(group)
                if (group.state, group.effective_plan_id) != before:
                    restored.append(group.id)
        if restored:
            _log(regform.event, user, LogKind.change,
                 f'Restored the group rate for {len(restored)} group(s) on "{regform.title}"')
        return {'restored_group_ids': restored}

    @registry.tool()
    def indico_group_registration_balances(user, event_id: PositiveId, reg_form_id: PositiveId,
                                           limit: PageSize = 50, offset: Offset = 0):
        """List outstanding group balances by registration for an organizer."""
        regform = _manager_form(user, event_id, reg_form_id)
        rows = [(member.balance_due, member) for member in balance_members(regform).all()
                if member.registration and not member.registration.is_deleted]
        rows = sorted((row for row in rows if row[0] > 0), key=lambda row: (-row[0], row[1].registration_id))
        return {'balances': [{'registration_id': member.registration_id, 'group_id': member.group_id,
                              'name': member.registration.full_name, 'currency': member.registration.currency,
                              'balance_due': str(amount)} for amount, member in rows[offset:offset + limit]],
                'currency': regform.currency, 'total_due': str(sum(amount for amount, _ in rows)),
                'next_offset': offset + limit if len(rows) > offset + limit else None}

    @registry.tool(read_only=False, idempotent=True)
    def indico_group_registration_refresh_balances(user, event_id: PositiveId, reg_form_id: PositiveId):
        """Resynchronize payment states to reflect recorded group balances on a registration form."""
        regform = _manager_form(user, event_id, reg_form_id, write=True)
        (group_query(regform).order_by(RegistrationGroup.id).enable_eagerloads(False)
         .with_for_update(of=RegistrationGroup).all())
        changed = 0
        with plugin.plugin_context(), pricing_in_progress():
            for member in balance_members(regform).all():
                registration = member.registration
                if registration is None or registration.is_deleted:
                    continue
                before = registration.state
                sync_balance_state(registration)
                changed += registration.state != before
        return {'changed_registrations': changed}

    @registry.tool(read_only=False, destructive=True, open_world=True)
    def indico_group_registration_record_balance_payment(
        user, event_id: PositiveId, reg_form_id: PositiveId, registration_id: PositiveId,
        expected_amount: Annotated[str, Field(description='Exact balance_due string from the balances tool')],
        confirm: Confirmation,
    ):
        """Record a group balance already paid offline and send the registration state update. No payment is taken."""
        _check_confirmation(confirm)
        regform = _manager_form(user, event_id, reg_form_id, write=True)
        registration = (Registration.query.filter_by(id=registration_id, registration_form_id=regform.id,
                                                     is_deleted=False).one_or_none())
        if registration is None or registration.group_membership is None:
            raise NotFound('Grouped registration not found in this form.')
        group = group_record(regform, registration.group_membership.group_id, lock=True)
        registration = (Registration.query.filter_by(id=registration_id, registration_form_id=regform.id,
                                                     is_deleted=False).populate_existing().enable_eagerloads(False)
                        .with_for_update(of=Registration).one_or_none())
        if registration is None:
            raise NotFound('Grouped registration not found in this form.')
        db.session.expire(registration, ['group_membership', 'transaction'])
        member = registration.group_membership
        if member is None or member.group_id != group.id or member.balance_due <= 0:
            raise BadRequest('This registration has no outstanding group balance.')
        if str(member.balance_due) != expected_amount:
            raise BadRequest('The outstanding balance has changed. Read the balance before recording payment.')
        with plugin.plugin_context():
            amount = record_balance_payment(registration, user)
            _log(regform.event, user, LogKind.positive,
                 f'Recorded a group balance payment of {amount} {registration.currency} '
                 f'for #{registration.friendly_id}',
                 meta={'registration_id': registration.id})
            notify_registration_state_update(registration, from_management=True)
        return {'registration_id': registration.id, 'amount': str(amount), 'currency': registration.currency}

    def reminder_data(regform, subject, body, sender_address, *, copy_for_sender=False):
        settings = get_group_settings(regform)
        if settings is None:
            raise NotFound('Group registration is not configured for this form.')
        recipients = _forming_group_members(regform)
        with regform.event.force_event_locale():
            subject = default_subject() if subject is None else subject
            body = default_body() if body is None else body
        sender_address = sender_address or next(iter(regform.event.get_allowed_sender_emails()), None)
        formdata = MultiDict({'subject': subject, 'body': body, 'sender_address': sender_address,
                              'submitted': '1', 'registration_id': [str(reg.id) for reg in recipients]})
        if copy_for_sender:
            formdata.add('copy_for_sender', 'y')
        form = EmailRegistrantsForm(formdata=formdata, regform=regform, csrf_enabled=False,
                                    recipients=[reg.email for reg in recipients])
        del form.attach_ticket
        if not form.validate():
            raise BadRequest(_format_form_errors(form))
        return settings, recipients, form

    @registry.tool()
    def indico_group_registration_preview_reminder(user, event_id: PositiveId, reg_form_id: PositiveId,
                                                   subject: str | None = None, body: str | None = None,
                                                   sender_address: str | None = None):
        """Preview the reminder to active members of forming groups, including the current recipient count."""
        regform = _manager_form(user, event_id, reg_form_id)
        with plugin.plugin_context():
            settings, recipients, form = reminder_data(regform, subject, body, sender_address)
            data = {'recipient_count': len(recipients), 'deadline': _iso(settings.get_reconciliation_dt()),
                    'subject_template': form.subject.data, 'body_template': form.body.data,
                    'allowed_sender_addresses': list(regform.event.get_allowed_sender_emails())}
            if recipients:
                preview = {'regform': regform, 'registration': recipients[0]}
                subject = replace_placeholders('registration-email', form.subject.data, **preview)
                body = replace_placeholders('registration-email', form.body.data, **preview)
                with regform.event.force_event_locale(recipients[0].user):
                    template = get_template_module('events/registration/emails/custom_email.html',
                                                   email_subject=subject, email_body=body)
                    token = str(recipients[0].uuid)
                    data.update(preview_subject=str(template.get_subject()).replace(token, '[registration-token]'),
                                preview_body=str(template.get_body()).replace(token, '[registration-token]'))
        return data

    @registry.tool(read_only=False, destructive=True, open_world=True)
    def indico_group_registration_send_reminder(
        user, event_id: PositiveId, reg_form_id: PositiveId, expected_recipient_count: PositiveId,
        confirm: Confirmation, subject: str | None = None, body: str | None = None,
        sender_address: str | None = None, copy_for_sender: bool = False,
    ):
        """Email active members of every forming group on a form. Preview the message and recipient count first."""
        _check_confirmation(confirm)
        regform = _manager_form(user, event_id, reg_form_id, write=True)
        with plugin.plugin_context():
            _, recipients, form = reminder_data(regform, subject, body, sender_address, copy_for_sender=copy_for_sender)
            if len(recipients) != expected_recipient_count:
                raise BadRequest('The reminder recipient count has changed. Preview the reminder again before sending.')
            handler = RHRegistrationEmailRegistrants()
            handler.event = regform.event
            handler.regform = regform
            handler.registrations = recipients
            handler._send_emails(form)
        _log(regform.event, user, LogKind.other,
             f'Reminded {len(recipients)} member(s) of forming groups on "{regform.title}"')
        return {'queued': len(recipients)}


def _format_form_errors(form):
    return '; '.join(f'{field}: {", ".join(map(str, errors))}' for field, errors in form.errors.items())


def _register_sentry_tools(registry, plugin):
    import sentry_sdk
    from indico_sentry.client import is_sending, pending_restart_settings, reinit_sentry, status
    from indico_sentry.forms import SettingsForm
    from indico_sentry.frontend import frontend_summary

    @registry.tool()
    def indico_sentry_status(user):
        """Show the current worker's Sentry status, enabled integrations and settings pending reload (admin only)."""
        _check_admin(user)
        with plugin.plugin_context():
            saved = plugin.settings.get_all()
            browser = frontend_summary(saved)
            pending = pending_restart_settings(saved)
        return {**{key: status[key] for key in ('initialized', 'dsn_configured', 'dsn_source', 'dsn_host',
                                               'environment', 'release', 'integrations', 'plugin_version')},
                'active': is_sending(), 'initialization_failed': bool(status['error']), 'pending_reload': pending,
                'browser': {key: browser[key] for key in ('enabled', 'requested', 'dsn_host', 'environment',
                                                         'release', 'sdk_version', 'traces_sample_rate', 'replay')},
                'settings_url': url_for('plugins.details', plugin=plugin.name, _external=True)}

    @registry.tool()
    def indico_sentry_settings(user):
        """Read Sentry's saved integration, sampling, and filtering settings exposed to MCP administrators."""
        _check_admin(user)
        with plugin.plugin_context():
            saved = plugin.settings.get_all()
        return {key: saved[key] for key in SentrySettingsUpdate.model_fields}

    @registry.tool(read_only=False, destructive=True, idempotent=True, open_world=True)
    def indico_sentry_update_settings(user, settings: SentrySettingsUpdate, confirm: Confirmation):
        """Update selected Sentry integration, sampling and filtering settings (admin only)."""
        _check_admin(user)
        _check_confirmation(confirm)
        updates = settings.model_dump(exclude_unset=True)
        if not updates:
            raise BadRequest('Provide at least one Sentry setting to update.')
        for key, value in updates.items():
            if isinstance(value, list):
                updates[key] = [line.strip() for item in value for line in item.split('\n') if line.strip()]
        with plugin.plugin_context():
            current = plugin.settings.get_all() | updates
            form = SettingsForm(formdata=None, data=current, csrf_enabled=False)
            if not form.validate():
                raise BadRequest('The Sentry settings did not pass the native settings form validation.')
            values = {key: form[key].data for key in updates}
            plugin.settings.set_multi(values)
        return {'updated': sorted(values), 'pending_reload': pending_restart_settings(current | values)}

    @registry.tool(read_only=False, destructive=False, open_world=True)
    def indico_sentry_send_test(user, confirm: Confirmation):
        """Send a fixed diagnostic message to the Sentry project already configured for this worker (admin only)."""
        _check_admin(user)
        _check_confirmation(confirm)
        if not is_sending():
            raise BadRequest('Sentry is inactive in this worker. Check the configured DSN and worker state.')
        with sentry_sdk.new_scope() as scope:
            scope.set_tag('indico.test_event', 'true')
            scope.set_level('info')
            event_id = sentry_sdk.capture_message('Indico Sentry plugin test event')
        sentry_sdk.get_client().flush(timeout=5.0)
        if not event_id:
            raise BadRequest('Sentry did not accept the diagnostic event.')
        return {'event_id': event_id}

    @registry.tool(read_only=False, destructive=False, idempotent=True, open_world=True)
    def indico_sentry_reload_worker(user, confirm: Confirmation):
        """Reload Sentry from saved configuration in the worker handling this call (admin only)."""
        _check_admin(user)
        _check_confirmation(confirm)
        with plugin.plugin_context():
            if not reinit_sentry():
                raise BadRequest('Sentry could not reload. See the Indico worker logs.')
        return {'reloaded': True, 'scope': 'current worker', 'active': is_sending()}
