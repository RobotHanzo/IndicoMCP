"""Tools for browsing and managing Indico categories and events."""

from datetime import datetime
from typing import Annotated, Literal

from indico.core.config import config
from indico.core.db.sqlalchemy.protection import ProtectionMode
from indico.core.db.sqlalchemy.util.queries import get_n_matching
from indico.modules.categories import Category
from indico.modules.categories.operations import create_category, delete_category, move_category, update_category
from indico.modules.categories.util import can_create_unlisted_events
from indico.modules.events import Event
from indico.modules.events.features.util import (
    get_disallowed_features,
    get_enabled_features,
    get_feature_definitions,
    set_feature_enabled,
)
from indico.modules.events.models.events import EventType
from indico.modules.events.notifications import notify_event_creation, notify_move_request_creation
from indico.modules.events.operations import (
    create_event,
    create_event_request,
    lock_event,
    unlock_event,
    update_event,
    update_event_type,
)
from indico.modules.events.util import track_location_changes, track_time_changes
from indico.modules.logs.models.entries import EventLogRealm, LogKind
from indico.util.string import natural_sort_key, sanitize_html
from pydantic import Field, StringConstraints
from pytz import common_timezones_set, utc
from werkzeug.exceptions import BadRequest, Forbidden, NotFound

ObjectID = Annotated[int, Field(ge=1, le=2147483647)]
CategoryID = Annotated[int, Field(ge=0, le=2147483647)]
PageSize = Annotated[int, Field(ge=1, le=100)]
Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500, pattern=r'^[^\x00]*$')]
Description = Annotated[str, StringConstraints(max_length=50000, pattern=r'^[^\x00]*$')]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=500, pattern=r'^[^\x00]*$')]
Timestamp = Annotated[str, Field(
    pattern=(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?'
             r'(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)$'),
    description='ISO 8601 date and time with an explicit UTC offset.',
)]
EventKind = Literal['lecture', 'meeting', 'conference']


def parse_timestamp(value):
    try:
        dt = datetime.fromisoformat(value)
        if dt.utcoffset() is not None:
            return dt.astimezone(utc)
    except (ValueError, OverflowError):
        pass
    raise BadRequest('Use an ISO 8601 date and time with a UTC offset.')


def get_category(category_id):
    category = Category.get(category_id, is_deleted=False) if category_id else Category.get_root()
    if category is None:
        raise NotFound('The category does not exist.')
    return category


def get_event(event_id):
    event = Event.get(event_id, is_deleted=False)
    if event is None:
        raise NotFound('The event does not exist.')
    return event


def require_access(obj, user):
    if not obj.can_access(user):
        raise Forbidden('You do not have access to this item.')
    return obj


def require_manage(obj, user, permission=None):
    if not obj.can_manage(user, permission=permission):
        raise Forbidden('You do not have permission to manage this item.')
    return obj


def require_unlocked(event):
    if event.is_locked:
        raise Forbidden('The event is locked.')


def require_confirmation(confirm):
    if not confirm:
        raise BadRequest('Confirm this action by setting confirm to true.')


def manage_event(event_id, user, permission=None):
    event = require_manage(get_event(event_id), user, permission=permission)
    require_unlocked(event)
    return event


def get_page(query, model, after_id, limit, visible, serialize):
    query = query.filter(model.id > after_id).order_by(None).order_by(model.id)
    matches = get_n_matching(query, limit + 1, visible, prefetch_factor=2)
    return {
        'items': [serialize(item) for item in matches[:limit]],
        'next_after_id': matches[limit - 1].id if len(matches) > limit else None,
    }


def serialize_category(category, user, *, detail=False):
    result = {
        'id': category.id,
        'title': category.title,
        'url': category.external_url,
        'can_manage': category.can_manage(user),
        'can_create_events': bool(category.can_create_events(user)),
    }
    if detail:
        parent = category.parent
        result.update(
            description=str(category.description),
            timezone=category.timezone,
            parent_id=parent.id if parent is not None and parent.can_access(user) else None,
        )
    return result


def serialize_event(event, user, *, detail=False):
    category = event.category
    result = {
        'id': event.id,
        'title': event.title,
        'type': event.type_.name,
        'url': event.external_url,
        'category_id': category.id if category is not None and category.can_access(user) else None,
        'start_dt': event.start_dt.isoformat(),
        'end_dt': event.end_dt.isoformat(),
        'timezone': event.timezone,
        'can_manage': event.can_manage(user),
        'is_locked': event.is_locked,
    }
    if detail:
        location = event.location_data
        result.update(
            description_html=sanitize_html(str(event.description)),
            venue=location['venue_name'],
            room=location['room_name'],
            address=location['address'],
        )
    return result


def _validate_timezone(value):
    if value not in common_timezones_set:
        raise BadRequest('Choose a supported IANA timezone.')
    return value


def _validate_dates(start, end):
    if start >= end:
        raise BadRequest('The end time must be later than the start time.')


def _update_dates(event, start_dt, end_dt, shift_timetable):
    start = parse_timestamp(start_dt) if start_dt is not None else event.start_dt
    end = parse_timestamp(end_dt) if end_dt is not None else event.end_dt
    _validate_dates(start, end)
    # Match the event date form's timetable boundary validation. Lectures
    # also shift any timetable data retained from a previous event type.
    shift_timetable = shift_timetable or event.type_ == EventType.lecture
    for entry in event.timetable_entries.filter_by(parent_id=None):
        if shift_timetable:
            fits = event.start_dt <= entry.start_dt and entry.end_dt - event.start_dt <= end - start
        else:
            fits = start <= entry.start_dt and entry.end_dt <= end
        if not fits:
            raise BadRequest('The event dates must include all timetable entries.')
    data = {}
    if start_dt is not None and start != event.start_dt:
        data['start_dt'] = start
    if end_dt is not None and end != event.end_dt:
        data['end_dt'] = end
    return data


def register_tools(registry):
    @registry.tool()
    def indico_list_categories(user, parent_id: CategoryID = 0, after_id: CategoryID = 0, limit: PageSize = 30):
        """List accessible immediate subcategories. Use parent_id 0 for the root and next_after_id for pagination."""
        parent = require_access(get_category(parent_id), user)
        query = Category.query.filter_by(parent_id=parent.id, is_deleted=False)
        return get_page(query, Category, after_id, limit, lambda item: item.can_access(user),
                        lambda item: serialize_category(item, user))

    @registry.tool()
    def indico_get_category(user, category_id: CategoryID = 0):
        """Get an accessible category's title, description, timezone, and link."""
        return serialize_category(require_access(get_category(category_id), user), user, detail=True)

    @registry.tool(read_only=False)
    def indico_create_category(user, parent_id: CategoryID, title: Title, description: Description = ''):
        """Create a subcategory in a category you manage. The description uses Markdown."""
        parent = require_manage(get_category(parent_id), user)

        def sort_key(category):
            return natural_sort_key(category.title), category.id

        previous = list(parent.children)
        ascending = previous == sorted(previous, key=sort_key)
        descending = not ascending and previous == sorted(previous, key=sort_key, reverse=True)
        category = create_category(parent, {'title': title, 'description': description})
        if ascending or descending:
            siblings = sorted(parent.children, key=sort_key, reverse=descending)
            for position, sibling in enumerate(siblings, start=1):
                sibling.position = position
        return serialize_category(category, user, detail=True)

    @registry.tool(read_only=False, idempotent=True)
    def indico_update_category(user, category_id: CategoryID, title: Title | None = None,
                               description: Description | None = None, timezone: ShortText | None = None):
        """Edit a category's title, Markdown description, or default timezone."""
        category = require_manage(get_category(category_id), user)
        data = {'title': title, 'description': description, 'timezone': timezone}
        if timezone is not None:
            data['timezone'] = _validate_timezone(timezone)
        if data := {key: value for key, value in data.items() if value is not None}:
            update_category(category, data)
        return serialize_category(category, user, detail=True)

    @registry.tool(read_only=False, idempotent=True)
    def indico_move_category(user, category_id: CategoryID, target_parent_id: CategoryID):
        """Move a category under another category. Both categories require management permission."""
        category = require_manage(get_category(category_id), user)
        target = require_manage(get_category(target_parent_id), user)
        if (category.is_root or category == target
                or target.parent_chain_query.filter(Category.id == category.id).has_rows()):
            raise BadRequest('The root cannot be moved, and a category cannot be moved into itself or a descendant.')
        if category.parent != target:
            move_category(category, target)
        return serialize_category(category, user, detail=True)

    @registry.tool(read_only=False, destructive=True)
    def indico_delete_category(user, category_id: CategoryID, confirm: bool):
        """Delete an empty non-root category you manage."""
        require_confirmation(confirm)
        category = require_manage(get_category(category_id), user)
        if category.is_root or not category.is_empty:
            raise BadRequest('Only empty non-root categories can be deleted.')
        delete_category(category)
        return {'id': category.id, 'deleted': True}

    @registry.tool()
    def indico_list_events(user, category_id: CategoryID = 0, include_descendants: bool = False,
                           title_contains: Title | None = None, window_start: Timestamp | None = None,
                           window_end: Timestamp | None = None, after_id: CategoryID = 0, limit: PageSize = 30):
        """List accessible events in a category by ID. An optional time range filters for overlapping events."""
        category = require_access(get_category(category_id), user)
        query = Event.query.filter_by(is_deleted=False)
        if include_descendants:
            query = query.filter(Event.is_visible_in(category.id))
        else:
            query = query.filter_by(category_id=category.id)
        if title_contains is not None:
            escaped = title_contains.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
            query = query.filter(Event.title.ilike(f'%{escaped}%', escape='\\'))
        start = parse_timestamp(window_start) if window_start is not None else None
        end = parse_timestamp(window_end) if window_end is not None else None
        if start is not None:
            query = query.filter(Event.end_dt >= start)
        if end is not None:
            query = query.filter(Event.start_dt <= end)
        if start is not None and end is not None:
            _validate_dates(start, end)

        def visible(event):
            return (event.can_access(user) and event.can_display(user) and
                    (event.category == category or event.category.can_access(user)))

        return get_page(query, Event, after_id, limit, visible, lambda item: serialize_event(item, user))

    @registry.tool()
    def indico_get_event(user, event_id: ObjectID):
        """Get an accessible event's type, title, dates, location, description, and link."""
        return serialize_event(require_access(get_event(event_id), user), user, detail=True)

    @registry.tool(read_only=False, open_world=True)
    def indico_create_event(user, title: Title, event_type: EventKind, start_dt: Timestamp, end_dt: Timestamp,
                            category_id: CategoryID | None = None, timezone: ShortText | None = None,
                            description_html: Description = '',
                            protection: Literal['inheriting', 'protected', 'public'] = 'inheriting'):
        """Create an event and send configured category notifications. Omit category_id to create an unlisted event."""
        category = get_category(category_id) if category_id is not None else None
        if category is not None and not category.can_create_events(user):
            raise Forbidden('You are not allowed to create events in this category.')
        if category is None and not can_create_unlisted_events(user):
            raise Forbidden('You are not allowed to create unlisted events.')
        if category is None and protection != 'inheriting':
            raise BadRequest('Unlisted events require inheriting protection.')
        start, end = parse_timestamp(start_dt), parse_timestamp(end_dt)
        _validate_dates(start, end)
        tz = _validate_timezone(timezone) if timezone is not None else (
            category.timezone if category is not None else config.DEFAULT_TIMEZONE)
        event = create_event(category, EventType[event_type], {
            'title': title, 'start_dt': start, 'end_dt': end, 'timezone': tz,
            'description': sanitize_html(description_html), 'protection_mode': ProtectionMode[protection],
        })
        notify_event_creation(event)
        return serialize_event(event, user, detail=True)

    @registry.tool(read_only=False, idempotent=True)
    def indico_update_event(user, event_id: ObjectID, title: Title | None = None,
                            description_html: Description | None = None, start_dt: Timestamp | None = None,
                            end_dt: Timestamp | None = None, timezone: ShortText | None = None,
                            shift_timetable: bool = True):
        """Update an unlocked event. By default, a changed start time shifts its timetable by the same amount."""
        event = manage_event(event_id, user)
        data = {}
        if title is not None:
            data['title'] = title
        if description_html is not None:
            data['description'] = sanitize_html(description_html)
        if timezone is not None:
            data['timezone'] = _validate_timezone(timezone)
        if start_dt is not None or end_dt is not None:
            data.update(_update_dates(event, start_dt, end_dt, shift_timetable))
        if data:
            with track_time_changes(user=user):
                update_event(event, update_timetable=shift_timetable, **data)
        return serialize_event(event, user, detail=True)

    @registry.tool(read_only=False, idempotent=True)
    def indico_update_event_location(user, event_id: ObjectID, venue: ShortText = '', room: ShortText = '',
                                     address: Description = ''):
        """Replace an unlocked event's location with a free-text venue, room, and address. Omitted fields clear."""
        event = manage_event(event_id, user)
        with track_location_changes():
            update_event(event, location_data={'inheriting': False, 'room_name': room, 'venue_name': venue,
                                               'address': address})
        return serialize_event(event, user, detail=True)

    @registry.tool(read_only=False, idempotent=True)
    def indico_set_event_type(user, event_id: ObjectID, event_type: EventKind):
        """Change an unlocked event's type to lecture, meeting, or conference."""
        event = manage_event(event_id, user)
        update_event_type(event, EventType[event_type])
        return serialize_event(event, user)

    @registry.tool()
    def indico_list_event_features(user, event_id: ObjectID):
        """List the available core and plugin features for an event you manage."""
        event = require_manage(get_event(event_id), user)
        allowed = get_feature_definitions().keys() - get_disallowed_features(event)
        enabled = get_enabled_features(event)
        definitions = get_feature_definitions()
        return {'items': [{'name': name, 'title': str(definitions[name].friendly_name), 'enabled': name in enabled}
                          for name in sorted(allowed)]}

    @registry.tool(read_only=False, idempotent=True, destructive=True)
    def indico_set_event_feature(user, event_id: ObjectID, name: ShortText, enabled: bool, confirm: bool = False):
        """Toggle an event feature and its native dependencies. Disabling requires confirmation."""
        event = manage_event(event_id, user)
        definitions = get_feature_definitions()
        if name not in definitions:
            raise NotFound('The event feature does not exist.')
        if enabled and name in get_disallowed_features(event):
            raise Forbidden('The feature is unavailable for this event.')
        if not enabled:
            require_confirmation(confirm)
        before = get_enabled_features(event)
        if set_feature_enabled(event, name, enabled):
            action = 'Enabled' if enabled else 'Disabled'
            kind = LogKind.positive if enabled else LogKind.negative
            event.log(EventLogRealm.management, kind, 'Features', f'{action} {definitions[name].friendly_name}', user)
        after = get_enabled_features(event)
        return {'name': name, 'enabled': name in after, 'changed': sorted(before ^ after)}

    @registry.tool(read_only=False, idempotent=True)
    def indico_set_event_locked(user, event_id: ObjectID, locked: bool):
        """Lock an event against edits or unlock it. Requires Indico's separate lock permission."""
        event = require_manage(get_event(event_id), user)
        if not event.can_lock(user):
            raise Forbidden('You do not have permission to lock or unlock this event.')
        if locked != event.is_locked:
            (lock_event if locked else unlock_event)(event)
        return serialize_event(event, user)

    @registry.tool(read_only=False, open_world=True)
    def indico_move_event(user, event_id: ObjectID, target_category_id: CategoryID, comment: Description = ''):
        """Move an event. Destinations that require approval receive a native move request and notifications."""
        event = manage_event(event_id, user)
        target = get_category(target_category_id)
        if not target.can_create_events(user) and not target.can_propose_events(user):
            raise Forbidden('You do not have permission to move events into this category.')
        if event.category == target:
            return {'event_id': event.id, 'category_id': target.id, 'status': 'already_in_category'}
        if target.can_create_events(user):
            event.move(target)
            return {'event_id': event.id, 'category_id': target.id, 'status': 'moved'}
        move_request = create_event_request(event, target, comment)
        notify_move_request_creation([event], target, comment)
        return {'event_id': event.id, 'category_id': target.id, 'status': 'pending', 'request_id': move_request.id}

    @registry.tool(read_only=False, destructive=True)
    def indico_withdraw_event_move_request(user, event_id: ObjectID, confirm: bool):
        """Withdraw the pending move request for an unlocked event you manage."""
        require_confirmation(confirm)
        event = manage_event(event_id, user)
        if event.pending_move_request is None:
            raise NotFound('The event has no pending move request.')
        event.pending_move_request.withdraw(user=user)
        return {'event_id': event.id, 'status': 'withdrawn'}

    @registry.tool(read_only=False, destructive=True)
    def indico_delete_event(user, event_id: ObjectID, confirm: bool):
        """Delete an unlocked event you manage."""
        require_confirmation(confirm)
        event = manage_event(event_id, user)
        event.delete('Deleted by user through MCP', user)
        return {'id': event.id, 'deleted': True}
