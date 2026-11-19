"""Tools for contributions, sessions, and the event timetable."""

from datetime import timedelta
from typing import Annotated

from indico.core.db.sqlalchemy.descriptions import RenderMode
from indico.modules.events.contributions import Contribution, contribution_settings
from indico.modules.events.contributions.models.fields import ContributionField
from indico.modules.events.contributions.models.subcontributions import SubContribution
from indico.modules.events.contributions.operations import (
    create_contribution,
    create_subcontribution,
    delete_contribution,
    delete_subcontribution,
    update_contribution,
    update_subcontribution,
)
from indico.modules.events.contributions.util import make_contribution_form
from indico.modules.events.layout.util import get_menu_entry_by_name
from indico.modules.events.models.events import EventType
from indico.modules.events.sessions.models.sessions import Session
from indico.modules.events.sessions.operations import (
    create_session,
    delete_session,
    delete_session_block,
    update_session,
    update_session_block,
)
from indico.modules.events.timetable.models.entries import TimetableEntry, TimetableEntryType
from indico.modules.events.timetable.operations import (
    create_break_entry,
    create_session_block_entry,
    delete_timetable_entry,
    schedule_contribution,
    update_break_entry,
    update_timetable_entry,
)
from indico.modules.events.util import get_random_color, track_time_changes
from indico.modules.logs.models.entries import EventLogRealm, LogKind
from indico.util.string import sanitize_html
from indico.web.flask.util import url_for
from pydantic import Field
from werkzeug.datastructures import MultiDict
from werkzeug.exceptions import BadRequest, Forbidden, NotFound

from indico_mcp.core import (
    CategoryID,
    Description,
    ObjectID,
    PageSize,
    ShortText,
    Timestamp,
    Title,
    get_event,
    get_page,
    manage_event,
    parse_timestamp,
    require_access,
    require_confirmation,
    require_manage,
    require_unlocked,
)

Minutes = Annotated[int, Field(ge=1, le=1440)]
CustomFields = Annotated[dict[str, Description | list[Description]], Field(max_length=100)]


def get_contribution(event, contribution_id):
    contribution = event.get_contribution(contribution_id)
    if contribution is None:
        raise NotFound('The contribution does not exist in this event.')
    return contribution


def get_subcontribution(contribution, subcontribution_id):
    return SubContribution.query.filter_by(id=subcontribution_id, contribution_id=contribution.id,
                                            is_deleted=False).first_or_404()


def get_session(event, session_id):
    event_session = event.get_session(session_id)
    if event_session is None:
        raise NotFound('The session does not exist in this event.')
    return event_session


def get_block(event, block_id):
    block = event.get_session_block(block_id)
    if block is None or block.timetable_entry is None:
        raise NotFound('The scheduled session block does not exist in this event.')
    return block


def contribution_is_visible(contribution, user, published, can_manage):
    return (contribution.can_access(user) and
            (published or can_manage or contribution.is_user_associated(user)))


def require_visible_contribution(contribution, user):
    require_access(contribution, user)
    event = contribution.event
    if not (contribution_settings.get(event, 'published') or event.can_manage(user, permission='contributions')
            or contribution.is_user_associated(user)):
        raise NotFound('The contributions have not been published.')
    return contribution


def _duration_minutes(item):
    return item.duration // timedelta(minutes=1)


def _serialize_contribution(contribution, user, *, detail=False):
    result = {
        'id': contribution.id,
        'friendly_id': contribution.friendly_id,
        'title': contribution.title,
        'code': contribution.code,
        'url': contribution.external_url,
        'session_id': (contribution.session_id if contribution.session is not None
                       and contribution.session.can_access(user) else None),
        'duration_minutes': _duration_minutes(contribution),
        'start_dt': contribution.start_dt.isoformat() if contribution.start_dt else None,
    }
    if detail:
        description = str(contribution.description)
        if contribution.render_mode == RenderMode.html:
            description = sanitize_html(description)
        result.update(description=description, description_format=contribution.render_mode.name)
    return result


def _serialize_subcontribution(subcontribution):
    description = str(subcontribution.description)
    if subcontribution.render_mode == RenderMode.html:
        description = sanitize_html(description)
    return {
        'id': subcontribution.id,
        'friendly_id': subcontribution.friendly_id,
        'contribution_id': subcontribution.contribution_id,
        'title': subcontribution.title,
        'code': subcontribution.code,
        'description': description,
        'description_format': subcontribution.render_mode.name,
        'duration_minutes': _duration_minutes(subcontribution),
        'url': url_for('contributions.display_subcontribution', subcontribution, _external=True),
    }


def _serialize_session(event_session, *, detail=False):
    result = {
        'id': event_session.id,
        'friendly_id': event_session.friendly_id,
        'title': event_session.title,
        'code': event_session.code,
        'url': url_for('sessions.display_session', event_session, _external=True),
    }
    if detail:
        duration = event_session.default_contribution_duration
        result.update(description=str(event_session.description),
                      default_contribution_duration_minutes=duration // timedelta(minutes=1))
    return result


def _serialize_timetable_entry(entry, user):
    obj = entry.object
    result = {
        'id': entry.id,
        'type': entry.type.name.lower(),
        'start_dt': entry.start_dt.isoformat(),
        'end_dt': entry.end_dt.isoformat(),
    }
    if obj.can_access(user):
        result.update(title=obj.full_title if entry.type == TimetableEntryType.SESSION_BLOCK else obj.title,
                      object_id=obj.id)
    if entry.parent is not None and entry.parent.object.can_access(user):
        result['parent_entry_id'] = entry.parent_id
    return result


def _validate_bounds(event, start, duration, block=None):
    lower, upper = (block.start_dt, block.end_dt) if block is not None else (event.start_dt, event.end_dt)
    if start < lower or start > upper or duration > upper - start:
        raise BadRequest('The entry must fit within its parent event or session block.')


def _custom_field_values(event, values):
    if not values:
        return {}
    formdata = MultiDict(values)
    form = make_contribution_form(event, only_custom_fields=True)(formdata=formdata, csrf_enabled=False)
    allowed = {field.name for field in form if field.name.startswith('custom_')}
    if not values.keys() <= allowed:
        raise BadRequest('Use active custom field names from indico_list_contribution_fields.')
    for field in list(form):
        if field.name not in values:
            delattr(form, field.name)
    if not form.validate():
        raise BadRequest('Invalid contribution custom fields: ' + ', '.join(sorted(form.errors)))
    return {name: form.data[name] for name in values}


def _content_changes(title, description, duration_minutes, code):
    data = {key: value for key, value in {'title': title, 'description': description, 'code': code}.items()
            if value is not None}
    if description is not None:
        data['render_mode'] = RenderMode.markdown
    if duration_minutes is not None:
        data['duration'] = timedelta(minutes=duration_minutes)
    return data


def register_tools(registry):
    @registry.tool()
    def indico_list_contributions(user, event_id: ObjectID, session_id: ObjectID | None = None,
                                  after_id: CategoryID = 0, limit: PageSize = 30):
        """List visible contributions in an event or session. Continue with next_after_id."""
        event = get_event(event_id)
        can_manage = event.can_manage(user, permission='contributions')
        if not can_manage:
            require_access(event, user)
            menu_entry = get_menu_entry_by_name('contributions', event)
            if menu_entry is not None and not menu_entry.is_enabled:
                raise Forbidden('The contribution list is disabled.')
        published = contribution_settings.get(event, 'published')
        query = Contribution.query.filter_by(event_id=event.id, is_deleted=False)
        if session_id is not None:
            require_access(get_session(event, session_id), user)
            query = query.filter_by(session_id=session_id)
        return get_page(query, Contribution, after_id, limit,
                        lambda item: contribution_is_visible(item, user, published, can_manage),
                        lambda item: _serialize_contribution(item, user))

    @registry.tool()
    def indico_get_contribution(user, event_id: ObjectID, contribution_id: ObjectID):
        """Get a visible contribution's details. The ID is the database ID returned by Indico tools."""
        contribution = require_visible_contribution(get_contribution(get_event(event_id), contribution_id), user)
        return _serialize_contribution(contribution, user, detail=True)

    @registry.tool()
    def indico_list_contribution_fields(user, event_id: ObjectID):
        """List active contribution custom fields and their configuration for an event you manage."""
        from indico.modules.events.contributions.schemas import ContributionFieldSchema

        event = require_manage(get_event(event_id), user, permission='contributions')
        fields = event.contribution_fields.filter_by(is_active=True).order_by(ContributionField.position).all()
        return {'items': [dict(field, input_name=f'custom_{field["id"]}')
                          for field in ContributionFieldSchema(many=True).dump(fields)]}

    @registry.tool()
    def indico_get_contribution_field_values(user, event_id: ObjectID, contribution_id: ObjectID):
        """Get the custom field values of a contribution you manage."""
        contribution = require_manage(get_contribution(get_event(event_id), contribution_id), user)
        return {f'custom_{field.contribution_field_id}': field.data for field in contribution.field_values}

    @registry.tool(read_only=False)
    def indico_create_contribution(user, event_id: ObjectID, title: Title, description: Description = '',
                                   duration_minutes: Minutes | None = None, code: ShortText = '',
                                   custom_fields: CustomFields | None = None):
        """Create an unscheduled contribution using Markdown and optional native custom field values."""
        event = manage_event(event_id, user, permission='contributions')
        duration = (timedelta(minutes=duration_minutes) if duration_minutes is not None
                    else contribution_settings.get(event, 'default_duration'))
        data = {'title': title, 'description': description, 'render_mode': RenderMode.markdown,
                'duration': duration, 'code': code, 'location_data': {'inheriting': True}}
        contribution = create_contribution(event, data, _custom_field_values(event, custom_fields))
        return _serialize_contribution(contribution, user, detail=True)

    @registry.tool(read_only=False, idempotent=True)
    def indico_update_contribution(user, event_id: ObjectID, contribution_id: ObjectID, title: Title | None = None,
                                   description: Description | None = None, duration_minutes: Minutes | None = None,
                                   code: ShortText | None = None, custom_fields: CustomFields | None = None):
        """Edit an unlocked contribution. Eligible submitters can edit title and Markdown description."""
        event = get_event(event_id)
        contribution = get_contribution(event, contribution_id)
        require_unlocked(event)
        if not contribution.can_edit(user):
            raise Forbidden('You do not have permission to edit this contribution.')
        if any(value is not None for value in (duration_minutes, code, custom_fields)):
            require_manage(contribution, user)
        data = _content_changes(title, description, duration_minutes, code)
        entry = contribution.timetable_entry
        if entry is not None and duration_minutes is not None:
            _validate_bounds(event, entry.start_dt, data['duration'], contribution.session_block)
        field_values = _custom_field_values(event, custom_fields)
        if data or field_values:
            with track_time_changes(user=user):
                update_contribution(contribution, data, field_values)
        return _serialize_contribution(contribution, user, detail=True)

    @registry.tool(read_only=False, idempotent=True, destructive=True)
    def indico_assign_contribution_session(user, event_id: ObjectID, contribution_id: ObjectID,
                                           session_id: ObjectID | None = None, confirm: bool = False):
        """Assign a contribution to a session, or clear that assignment. This can unschedule the contribution."""
        event = manage_event(event_id, user)
        contribution = get_contribution(event, contribution_id)
        event_session = get_session(event, session_id) if session_id is not None else None
        if contribution.session != event_session and contribution.timetable_entry is not None:
            require_confirmation(confirm)
        result = (update_contribution(contribution, {'session': event_session})
                  if contribution.session != event_session else {'unscheduled': False})
        return {'id': contribution.id, 'session_id': session_id, 'unscheduled': result['unscheduled']}

    @registry.tool(read_only=False, destructive=True)
    def indico_delete_contribution(user, event_id: ObjectID, contribution_id: ObjectID, confirm: bool):
        """Delete an unlocked contribution you manage. This removes its timetable entry as well."""
        require_confirmation(confirm)
        event = get_event(event_id)
        contribution = require_manage(get_contribution(event, contribution_id), user)
        require_unlocked(event)
        delete_contribution(contribution)
        return {'id': contribution.id, 'deleted': True}

    @registry.tool(read_only=False, idempotent=True, destructive=True)
    def indico_set_contributions_published(user, event_id: ObjectID, published: bool, confirm: bool = False):
        """Publish or unpublish the contributions and timetable of an unlocked event you manage."""
        if not published:
            require_confirmation(confirm)
        event = manage_event(event_id, user)
        if contribution_settings.get(event, 'published') != published:
            contribution_settings.set(event, 'published', published)
            kind = LogKind.positive if published else LogKind.negative
            message = 'Contributions published' if published else 'Contributions unpublished'
            event.log(EventLogRealm.management, kind, 'Contributions', message, user)
        return {'event_id': event.id, 'published': published}

    @registry.tool()
    def indico_list_subcontributions(user, event_id: ObjectID, contribution_id: ObjectID,
                                     after_id: CategoryID = 0, limit: PageSize = 30):
        """List accessible subcontributions of a visible contribution."""
        contribution = require_visible_contribution(get_contribution(get_event(event_id), contribution_id), user)
        query = SubContribution.query.filter_by(contribution_id=contribution.id, is_deleted=False)
        return get_page(query, SubContribution, after_id, limit, lambda item: item.can_access(user),
                        _serialize_subcontribution)

    @registry.tool(read_only=False)
    def indico_create_subcontribution(user, event_id: ObjectID, contribution_id: ObjectID, title: Title,
                                      description: Description = '', duration_minutes: Minutes = 20,
                                      code: ShortText = ''):
        """Create a subcontribution under an unlocked contribution you manage."""
        event = get_event(event_id)
        contribution = require_manage(get_contribution(event, contribution_id), user)
        require_unlocked(event)
        data = _content_changes(title, description, duration_minutes, code)
        return _serialize_subcontribution(create_subcontribution(contribution, data))

    @registry.tool(read_only=False, idempotent=True)
    def indico_update_subcontribution(user, event_id: ObjectID, contribution_id: ObjectID, subcontribution_id: ObjectID,
                                      title: Title | None = None, description: Description | None = None,
                                      duration_minutes: Minutes | None = None, code: ShortText | None = None):
        """Edit an unlocked subcontribution. Eligible submitters can edit title and Markdown description."""
        event = get_event(event_id)
        sub = get_subcontribution(get_contribution(event, contribution_id), subcontribution_id)
        require_unlocked(event)
        if not sub.can_edit(user):
            raise Forbidden('You do not have permission to edit this subcontribution.')
        if duration_minutes is not None or code is not None:
            require_manage(sub, user)
        if data := _content_changes(title, description, duration_minutes, code):
            update_subcontribution(sub, data)
        return _serialize_subcontribution(sub)

    @registry.tool(read_only=False, destructive=True)
    def indico_delete_subcontribution(user, event_id: ObjectID, contribution_id: ObjectID,
                                      subcontribution_id: ObjectID, confirm: bool):
        """Delete a subcontribution of an unlocked contribution you manage."""
        require_confirmation(confirm)
        event = get_event(event_id)
        sub = require_manage(get_subcontribution(get_contribution(event, contribution_id), subcontribution_id), user)
        require_unlocked(event)
        delete_subcontribution(sub)
        return {'id': sub.id, 'deleted': True}

    @registry.tool()
    def indico_list_sessions(user, event_id: ObjectID, after_id: CategoryID = 0, limit: PageSize = 30):
        """List accessible sessions in a published event timetable or an event you manage."""
        event = get_event(event_id)
        if not event.can_manage(user):
            require_access(event, user)
            if not contribution_settings.get(event, 'published'):
                raise NotFound('The timetable has not been published.')
        query = Session.query.filter_by(event_id=event.id, is_deleted=False)
        return get_page(query, Session, after_id, limit, lambda item: item.can_access(user), _serialize_session)

    @registry.tool()
    def indico_get_session(user, event_id: ObjectID, session_id: ObjectID):
        """Get an accessible session and its default contribution duration."""
        event_session = require_access(get_session(get_event(event_id), session_id), user)
        return _serialize_session(event_session, detail=True)

    @registry.tool(read_only=False)
    def indico_create_session(user, event_id: ObjectID, title: Title, description: Description = '',
                              code: ShortText = '', default_duration_minutes: Minutes | None = None):
        """Create a session in an unlocked event you manage."""
        event = manage_event(event_id, user)
        if event.type_ != EventType.conference and code:
            raise BadRequest('Session codes are available only for conferences.')
        duration = (timedelta(minutes=default_duration_minutes) if default_duration_minutes is not None
                    else contribution_settings.get(event, 'default_duration'))
        data = {'title': title, 'description': description, 'code': code,
                'default_contribution_duration': duration, 'location_data': {'inheriting': True},
                'colors': get_random_color(event)}
        return _serialize_session(create_session(event, data), detail=True)

    @registry.tool(read_only=False, idempotent=True)
    def indico_update_session(user, event_id: ObjectID, session_id: ObjectID, title: Title | None = None,
                              description: Description | None = None, code: ShortText | None = None,
                              default_duration_minutes: Minutes | None = None):
        """Edit an unlocked session you manage."""
        event = get_event(event_id)
        event_session = require_manage(get_session(event, session_id), user)
        require_unlocked(event)
        if code is not None and event.type_ != EventType.conference:
            raise BadRequest('Session codes are available only for conferences.')
        fields = {'title': title, 'description': description, 'code': code}
        data = {key: value for key, value in fields.items() if value is not None}
        if default_duration_minutes is not None:
            data['default_contribution_duration'] = timedelta(minutes=default_duration_minutes)
        if data:
            update_session(event_session, data)
        return _serialize_session(event_session, detail=True)

    @registry.tool(read_only=False, destructive=True)
    def indico_delete_session(user, event_id: ObjectID, session_id: ObjectID, confirm: bool):
        """Delete an unlocked session, remove its timetable blocks, and detach its contributions."""
        require_confirmation(confirm)
        event = get_event(event_id)
        event_session = require_manage(get_session(event, session_id), user)
        require_unlocked(event)
        delete_session(event_session)
        return {'id': event_session.id, 'deleted': True}

    @registry.tool()
    def indico_get_timetable(user, event_id: ObjectID, after_id: CategoryID = 0, limit: PageSize = 30):
        """List visible timetable entries with start and end times. Use next_after_id to page."""
        event = require_access(get_event(event_id), user)
        if not contribution_settings.get(event, 'published') and not event.can_manage(user):
            raise NotFound('The timetable has not been published.')
        return get_page(event.timetable_entries, TimetableEntry, after_id, limit, lambda item: item.can_view(user),
                        lambda item: _serialize_timetable_entry(item, user))

    @registry.tool(read_only=False)
    def indico_create_session_block(user, event_id: ObjectID, session_id: ObjectID, start_dt: Timestamp,
                                    duration_minutes: Minutes, title: ShortText = ''):
        """Add a scheduled block inside an unlocked event for a session whose blocks you manage."""
        event = get_event(event_id)
        event_session = get_session(event, session_id)
        require_unlocked(event)
        if not event_session.can_manage_blocks(user):
            raise Forbidden('You do not have permission to manage this session’s blocks.')
        start, duration = parse_timestamp(start_dt), timedelta(minutes=duration_minutes)
        _validate_bounds(event, start, duration)
        entry = create_session_block_entry(event_session, {'title': title, 'start_dt': start, 'duration': duration,
                                                           'location_data': {'inheriting': True}})
        return _serialize_timetable_entry(entry, user)

    @registry.tool(read_only=False, idempotent=True)
    def indico_update_session_block(user, event_id: ObjectID, block_id: ObjectID, title: ShortText | None = None,
                                    start_dt: Timestamp | None = None, duration_minutes: Minutes | None = None):
        """Update a session block and shift its nested entries when its start time changes."""
        event = get_event(event_id)
        block = get_block(event, block_id)
        require_unlocked(event)
        if not block.can_manage(user):
            raise Forbidden('You do not have permission to manage this session block.')
        start = parse_timestamp(start_dt) if start_dt is not None else block.start_dt
        duration = timedelta(minutes=duration_minutes) if duration_minutes is not None else block.duration
        _validate_bounds(event, start, duration)
        if any(entry.start_dt < block.start_dt or entry.end_dt - block.start_dt > duration
               for entry in block.timetable_entry.children):
            raise BadRequest('The block must fit its nested timetable entries.')
        fields = {'title': title, 'start_dt': start if start_dt is not None else None,
                  'duration': duration if duration_minutes is not None else None}
        if data := {key: value for key, value in fields.items() if value is not None}:
            with track_time_changes(user=user):
                update_session_block(block, data)
        return _serialize_timetable_entry(block.timetable_entry, user)

    @registry.tool(read_only=False, destructive=True)
    def indico_delete_session_block(user, event_id: ObjectID, block_id: ObjectID, confirm: bool):
        """Delete a session block, including breaks and the scheduled associations inside it."""
        require_confirmation(confirm)
        event = get_event(event_id)
        block = get_block(event, block_id)
        require_unlocked(event)
        if not block.can_manage(user):
            raise Forbidden('You do not have permission to manage this session block.')
        delete_session_block(block)
        return {'id': block_id, 'deleted': True}

    @registry.tool(read_only=False)
    def indico_schedule_contribution(user, event_id: ObjectID, contribution_id: ObjectID, start_dt: Timestamp,
                                     session_block_id: ObjectID | None = None, move_between_sessions: bool = False):
        """Schedule an unscheduled contribution. Specify a session block to place it within that block."""
        event = manage_event(event_id, user)
        contribution = get_contribution(event, contribution_id)
        if contribution.timetable_entry is not None:
            raise BadRequest('The contribution is already scheduled.')
        block = get_block(event, session_block_id) if session_block_id is not None else None
        target_session = block.session if block is not None else None
        if contribution.session is not None and contribution.session != target_session and not move_between_sessions:
            raise BadRequest('The contribution belongs to another session. Set move_between_sessions to reassign it.')
        start = parse_timestamp(start_dt)
        _validate_bounds(event, start, contribution.duration, block)
        if block is None and contribution.session is not None:
            update_contribution(contribution, {'session': None})
        return _serialize_timetable_entry(schedule_contribution(contribution, start, session_block=block), user)

    @registry.tool(read_only=False, idempotent=True)
    def indico_reschedule_contribution(user, event_id: ObjectID, contribution_id: ObjectID, start_dt: Timestamp):
        """Change a scheduled contribution's start time within its current timetable parent."""
        event = manage_event(event_id, user)
        contribution = get_contribution(event, contribution_id)
        entry = contribution.timetable_entry
        if entry is None:
            raise BadRequest('The contribution has no timetable entry.')
        start = parse_timestamp(start_dt)
        _validate_bounds(event, start, contribution.duration, contribution.session_block)
        with track_time_changes(user=user):
            update_timetable_entry(entry, {'start_dt': start})
        return _serialize_timetable_entry(entry, user)

    @registry.tool(read_only=False, idempotent=True, destructive=True)
    def indico_unschedule_contribution(user, event_id: ObjectID, contribution_id: ObjectID, confirm: bool):
        """Remove a conference contribution from the timetable while keeping it in the conference."""
        require_confirmation(confirm)
        event = manage_event(event_id, user)
        if event.type_ != EventType.conference:
            raise BadRequest('Unscheduling without deleting a contribution is available only for conferences.')
        contribution = get_contribution(event, contribution_id)
        if contribution.timetable_entry is not None:
            contribution.session_block = None
            delete_timetable_entry(contribution.timetable_entry)
        return {'id': contribution.id, 'scheduled': False}

    @registry.tool(read_only=False)
    def indico_create_timetable_break(user, event_id: ObjectID, title: Title, start_dt: Timestamp,
                                       duration_minutes: Minutes, description: Description = '',
                                       session_block_id: ObjectID | None = None):
        """Add a break to an unlocked event or one of its scheduled session blocks."""
        event = manage_event(event_id, user)
        block = get_block(event, session_block_id) if session_block_id is not None else None
        start, duration = parse_timestamp(start_dt), timedelta(minutes=duration_minutes)
        _validate_bounds(event, start, duration, block)
        data = {'start_dt': start, 'title': title, 'description': description, 'duration': duration,
                'location_data': {'inheriting': True}}
        return _serialize_timetable_entry(create_break_entry(event, data, session_block=block), user)

    @registry.tool(read_only=False, idempotent=True)
    def indico_update_timetable_break(user, event_id: ObjectID, entry_id: ObjectID, title: Title | None = None,
                                       start_dt: Timestamp | None = None, duration_minutes: Minutes | None = None,
                                       description: Description | None = None):
        """Edit an event timetable break identified by its timetable entry ID."""
        event = manage_event(event_id, user)
        entry = event.timetable_entries.filter_by(id=entry_id, type=TimetableEntryType.BREAK).first_or_404()
        start = parse_timestamp(start_dt) if start_dt is not None else entry.start_dt
        duration = timedelta(minutes=duration_minutes) if duration_minutes is not None else entry.duration
        _validate_bounds(event, start, duration, entry.parent.session_block if entry.parent is not None else None)
        fields = {'title': title, 'description': description, 'start_dt': start if start_dt is not None else None,
                  'duration': duration if duration_minutes is not None else None}
        if data := {key: value for key, value in fields.items() if value is not None}:
            with track_time_changes(user=user):
                update_break_entry(entry.break_, data)
        return _serialize_timetable_entry(entry, user)

    @registry.tool(read_only=False, destructive=True)
    def indico_delete_timetable_break(user, event_id: ObjectID, entry_id: ObjectID, confirm: bool):
        """Delete a break from an unlocked timetable using its timetable entry ID."""
        require_confirmation(confirm)
        event = manage_event(event_id, user)
        entry = event.timetable_entries.filter_by(id=entry_id, type=TimetableEntryType.BREAK).first_or_404()
        delete_timetable_entry(entry)
        return {'id': entry_id, 'deleted': True}
