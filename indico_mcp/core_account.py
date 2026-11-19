"""Tools for the authenticated user's profile, preferences, and dashboard."""

from typing import Annotated, Literal

from indico.core import signals
from indico.modules.users import user_management_settings
from indico.modules.users.util import get_linked_events, get_unlisted_events, log_user_update
from indico.util.i18n import get_all_locales
from indico.web.flask.util import url_for
from marshmallow import ValidationError
from pydantic import Field
from pytz import common_timezones_set
from werkzeug.exceptions import BadRequest, Forbidden

from indico_mcp.core import (
    CategoryID,
    PageSize,
    ShortText,
    Timestamp,
    Title,
    get_category,
    get_event,
    parse_timestamp,
    require_access,
    serialize_category,
    serialize_event,
)

ProfileTitle = Literal['none', 'mr', 'ms', 'mrs', 'dr', 'prof', 'mx']
CalendarMinutes = Annotated[int, Field(ge=0, le=2147483647)]
NameFormat = Literal['first_last', 'last_first', 'last_f', 'f_last', 'first_last_upper', 'last_first_upper',
                     'last_f_upper', 'f_last_upper']
PREFERENCE_KEYS = ('lang', 'timezone', 'force_language', 'force_timezone', 'show_future_events',
                   'show_past_events', 'name_format', 'use_previewer_pdf', 'add_ical_alerts',
                   'add_ical_alerts_mins', 'use_markdown_for_minutes')


def _serialize_profile(user):
    from indico.modules.users.schemas import UserPersonalDataSchema

    return dict(UserPersonalDataSchema().dump(user), id=user.id,
                url=url_for('users.user_profile', user, _external=True))


def _serialize_preferences(user):
    preferences = {key: user.settings.get(key) for key in PREFERENCE_KEYS}
    preferences['name_format'] = preferences['name_format'].name
    return preferences


def register_tools(registry):
    @registry.tool()
    def indico_get_my_profile(user):
        """Read your own Indico profile and see which fields are synchronized with your identity provider."""
        return _serialize_profile(user)

    @registry.tool(read_only=False, idempotent=True)
    def indico_update_my_profile(user, first_name: Title | None = None, last_name: Title | None = None,
                                 title: ProfileTitle | None = None, address: ShortText | None = None,
                                 phone: ShortText | None = None, affiliation: ShortText | None = None,
                                 affiliation_id: CategoryID | None = None):
        """Update your local Indico profile fields. Fields managed by your identity provider stay synchronized."""
        from indico.modules.users.schemas import UserPersonalDataSchema

        raw = {'first_name': first_name, 'last_name': last_name, 'title': title, 'address': address,
               'phone': phone, 'affiliation': affiliation, 'affiliation_id': affiliation_id}
        inputs = {key: value for key, value in raw.items() if value is not None}
        if not inputs:
            return _serialize_profile(user)
        if title == 'none':
            inputs['title'] = None
        if affiliation_id == 0:
            inputs['affiliation_id'] = None
        try:
            updates = UserPersonalDataSchema().load(inputs, partial=True)
        except ValidationError as exc:
            raise BadRequest('Invalid profile fields: ' + ', '.join(sorted(exc.messages))) from None
        if (updates.get('affiliation') and not updates.get('affiliation_link')
                and user_management_settings.get('only_predefined_affiliations')):
            raise BadRequest('Choose a predefined affiliation.')
        if user.synced_fields.intersection(updates):
            raise Forbidden('Use the Indico profile page to manage fields that sync with your identity provider.')
        changes = {}
        for name, value in updates.items():
            previous = getattr(user, name)
            if value != previous:
                setattr(user, name, value)
                changes[name] = (previous, value)
        if changes:
            log_user_update(user, changes)
        return _serialize_profile(user)

    @registry.tool()
    def indico_get_my_preferences(user):
        """Read your Indico display, timezone, and calendar preferences."""
        return _serialize_preferences(user)

    @registry.tool(read_only=False, idempotent=True)
    def indico_update_my_preferences(user, lang: ShortText | None = None, timezone: ShortText | None = None,
                                     force_language: bool | None = None, force_timezone: bool | None = None,
                                     show_future_events: bool | None = None, show_past_events: bool | None = None,
                                     name_format: NameFormat | None = None, use_previewer_pdf: bool | None = None,
                                     add_ical_alerts: bool | None = None,
                                     add_ical_alerts_mins: CalendarMinutes | None = None,
                                     use_markdown_for_minutes: bool | None = None):
        """Set one or more of your own display and calendar preferences."""
        from indico.modules.users.models.users import NameFormat as IndicoNameFormat

        if lang is not None and lang not in get_all_locales():
            raise BadRequest('Choose a language supported by Indico.')
        if timezone is not None and timezone not in common_timezones_set:
            raise BadRequest('Choose a supported IANA timezone.')
        if name_format is not None:
            name_format = IndicoNameFormat[name_format]
        values = {'lang': lang, 'timezone': timezone, 'force_language': force_language,
                  'force_timezone': force_timezone, 'show_future_events': show_future_events,
                  'show_past_events': show_past_events, 'name_format': name_format,
                  'use_previewer_pdf': use_previewer_pdf, 'add_ical_alerts': add_ical_alerts,
                  'add_ical_alerts_mins': add_ical_alerts_mins, 'use_markdown_for_minutes': use_markdown_for_minutes}
        user.settings.set_multi({key: value for key, value in values.items() if value is not None})
        return _serialize_preferences(user)

    @registry.tool()
    def indico_list_my_linked_events(user, from_dt: Timestamp | None = None, limit: PageSize = 30):
        """List events on your dashboard and your roles in those events."""
        dt = parse_timestamp(from_dt) if from_dt is not None else None
        linked = get_linked_events(user, dt, limit=limit)
        return {'items': [dict(serialize_event(event, user), roles=sorted(roles))
                          for event, roles in linked.items() if event.can_access(user)]}

    @registry.tool()
    def indico_list_my_unlisted_events(user, after_id: CategoryID = 0, limit: PageSize = 30):
        """List your unlisted events from the Indico dashboard."""
        items = sorted((event for event in get_unlisted_events(user)
                        if event.id > after_id and event.can_access(user)), key=lambda event: event.id)
        return {'items': [serialize_event(event, user) for event in items[:limit]],
                'next_after_id': items[limit - 1].id if len(items) > limit else None}

    @registry.tool()
    def indico_list_my_favorites(user, kind: Literal['category', 'event'], after_id: CategoryID = 0,
                                limit: PageSize = 30):
        """List your favorite categories or events that are still accessible."""
        objects = user.favorite_categories if kind == 'category' else user.favorite_events
        serialize = serialize_category if kind == 'category' else serialize_event
        accessible = (item for item in objects if item.id > after_id and not item.is_deleted and item.can_access(user))
        items = sorted(accessible, key=lambda item: item.id)
        return {'items': [serialize(item, user) for item in items[:limit]],
                'next_after_id': items[limit - 1].id if len(items) > limit else None}

    @registry.tool(read_only=False, idempotent=True)
    def indico_set_my_favorite(user, kind: Literal['category', 'event'], id: CategoryID, favorite: bool):
        """Add or remove an event or category from your own favorites."""
        item = get_category(id) if kind == 'category' else get_event(id)
        objects = user.favorite_categories if kind == 'category' else user.favorite_events
        if favorite and item not in objects:
            require_access(item, user)
            objects.add(item)
            signal = signals.users.favorite_category_added if kind == 'category' else signals.users.favorite_event_added
            signal.send(user, **{kind: item})
        elif not favorite and item in objects:
            objects.discard(item)
            signal = (signals.users.favorite_category_removed if kind == 'category'
                      else signals.users.favorite_event_removed)
            signal.send(user, **{kind: item})
        else:
            return {'id': item.id, 'favorite': favorite}
        if kind == 'category' and (suggestion := user.suggested_categories.filter_by(category=item).first()):
            user.suggested_categories.remove(suggestion)
        return {'id': item.id, 'favorite': favorite}
