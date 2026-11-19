"""Tools for an event's call for abstracts and abstract submissions."""

import json
from typing import Annotated, Literal

from indico.core.db.sqlalchemy.descriptions import RenderMode
from indico.core.db.sqlalchemy.util.session import no_autoflush
from indico.modules.events.abstracts.models.abstracts import Abstract, AbstractState
from indico.modules.events.abstracts.models.persons import AbstractPersonLink
from indico.modules.events.abstracts.operations import (
    close_cfa,
    create_abstract,
    delete_abstract,
    open_cfa,
    schedule_cfa,
    update_abstract,
    withdraw_abstract,
)
from indico.modules.events.abstracts.settings import abstracts_settings
from indico.modules.events.abstracts.util import make_abstract_form
from indico.modules.events.util import get_field_values
from indico.util.string import sanitize_html
from indico.web.flask.util import url_for
from indico.web.forms.base import FormDefaults
from pydantic import BaseModel, ConfigDict, Field
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
    parse_timestamp,
    require_access,
    require_confirmation,
    require_manage,
    require_unlocked,
)
from indico_mcp.core_schedule import CustomFields


class AbstractAuthor(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    first_name: ShortText = ''
    last_name: Title
    email: ShortText = ''
    affiliation: ShortText = ''
    affiliation_id: ObjectID | None = None
    role: Literal['primary', 'secondary'] = 'secondary'
    speaker: bool = False


Authors = Annotated[list[AbstractAuthor], Field(max_length=50)]
TrackIDs = Annotated[list[ObjectID], Field(max_length=100)]


def _get_event(event_id, user, *, manage=False, unlock=False):
    event = require_access(get_event(event_id), user)
    if not event.has_feature('abstracts'):
        raise NotFound('This event does not have a call for abstracts.')
    if manage:
        require_manage(event, user, permission='abstracts')
    if unlock:
        require_unlocked(event)
    return event


def _get_abstract(event, abstract_id):
    return Abstract.query.filter_by(event_id=event.id, id=abstract_id, is_deleted=False).first_or_404()


def _serialize_cfa(event, user):
    cfa = event.cfa
    return {'event_id': event.id, 'is_open': cfa.is_open, 'can_submit': bool(cfa.can_submit_abstracts(user)),
            'start_dt': cfa.start_dt.isoformat() if cfa.start_dt else None,
            'end_dt': cfa.end_dt.isoformat() if cfa.end_dt else None,
            'modification_end_dt': cfa.modification_end_dt.isoformat() if cfa.modification_end_dt else None,
            'url': url_for('abstracts.call_for_abstracts', event, _external=True)}


def _serialize_abstract(abstract, user, *, detail=False):
    management = abstract.state == AbstractState.invited and abstract.event.can_manage(user, permission='abstracts')
    result = {'id': abstract.id, 'friendly_id': abstract.friendly_id, 'event_id': abstract.event_id,
              'title': abstract.title, 'state': abstract.public_state.name,
              'url': url_for('abstracts.display_abstract', abstract, management=management, _external=True)}
    if detail:
        description = str(abstract.description)
        if abstract.render_mode == RenderMode.html:
            description = sanitize_html(description)
        result.update(description=description,
                      description_format=abstract.render_mode.name,
                      tracks=[{'id': track.id, 'title': track.title} for track in abstract.submitted_for_tracks],
                      authors=[{'name': person.display_full_name, 'affiliation': person.affiliation,
                                'role': person.author_type.name, 'speaker': person.is_speaker}
                               for person in abstract.person_links])
    return result


def _serialize_author(author):
    return {'first_name': author.first_name, 'last_name': author.last_name, 'email': author.email,
            'affiliation': author.affiliation, 'affiliation_id': author.affiliation_id,
            'roles': [author.role, 'speaker'] if author.speaker else [author.role]}


def _abstract_form_data(event, user, data, *, abstract=None, management=False):
    custom = data.pop('custom_fields', None) or {}
    if any(not name.startswith('custom_') for name in custom):
        raise BadRequest('Use custom field names from the abstract form.')
    data.update(custom)
    authors = data.pop('authors', None)
    tracks = data.pop('track_ids', None)
    contribution_type_id = data.pop('contribution_type_id', None)
    if tracks is not None:
        data['submitted_for_tracks'] = [str(track) for track in tracks] or ['__None']
    if contribution_type_id is not None:
        data['submitted_contrib_type'] = str(contribution_type_id)

    if authors is not None:
        data['person_links'] = json.dumps([_serialize_author(author) for author in authors])
    elif abstract is None:
        # Use the principal representation accepted by the native author widget.
        roles = ['primary']
        if (abstracts_settings.get(event, 'allow_speakers') and
                abstracts_settings.get(event, 'speakers_required')):
            roles.append('speaker')
        data['person_links'] = json.dumps([{
            'type': 'user', 'first_name': user.first_name, 'last_name': user.last_name, 'email': user.email,
            'affiliation': user.affiliation, 'affiliation_id': user.affiliation_id, 'roles': roles,
        }])

    defaults = None
    if abstract is not None:
        current = {f'custom_{value.contribution_field_id}': value.data for value in abstract.field_values}
        defaults = FormDefaults(abstract, **current)
    form_class = make_abstract_form(event, user, management=management)
    form = form_class(formdata=MultiDict(data), event=event, abstract=abstract, management=management,
                      invited=abstract is not None and abstract.state == AbstractState.invited,
                      obj=defaults, csrf_enabled=False)
    if hasattr(form, 'attachments'):
        del form.attachments
    if any(name not in form for name in data):
        raise BadRequest('One or more fields are disabled or unavailable in this abstract form.')
    if tracks is not None and form.track_field_disabled:
        raise Forbidden('The submitted tracks cannot be changed.')
    if authors and any(author.speaker for author in authors) and not form.person_links.allow_speakers:
        raise BadRequest('This abstract form does not allow speakers.')
    if not form.validate():
        raise BadRequest('Invalid abstract fields: ' + ', '.join(sorted(form.errors)))
    if abstract is None:
        return get_field_values(form.data)
    return get_field_values({name: form.data[name] for name in data})


def register_tools(registry):
    @registry.tool()
    def indico_get_call_for_abstracts(user, event_id: ObjectID):
        """Get submission dates, your submission eligibility, and the native call for abstracts link."""
        return _serialize_cfa(_get_event(event_id, user), user)

    @registry.tool()
    def indico_list_abstract_tracks(user, event_id: ObjectID):
        """List tracks and public contribution types available for abstract submission."""
        event = _get_event(event_id, user)
        return {'tracks': [{'id': track.id, 'title': track.title} for track in event.tracks],
                'allow_speakers': abstracts_settings.get(event, 'allow_speakers'),
                'speakers_required': abstracts_settings.get(event, 'speakers_required'),
                'allow_multiple_tracks': abstracts_settings.get(event, 'allow_multiple_tracks'),
                'tracks_required': abstracts_settings.get(event, 'tracks_required'),
                'contribution_type_required': abstracts_settings.get(event, 'contrib_type_required'),
                'description': abstracts_settings.get(event, 'description_settings'),
                'contribution_types': [{'id': kind.id, 'title': kind.name}
                                       for kind in event.contribution_types if not kind.is_private],
                'custom_fields': [{'input_name': f'custom_{field.id}', 'title': field.title,
                                   'type': field.field_type, 'required': field.is_required,
                                   'field_data': field.field_data}
                                  for field in event.contribution_fields if field.is_active and field.field is not None
                                  and field.is_user_editable]}

    @registry.tool()
    def indico_list_abstracts(user, event_id: ObjectID, after_id: CategoryID = 0, limit: PageSize = 30):
        """List the abstracts you submitted or co-authored. Abstract managers can list the whole event."""
        event = _get_event(event_id, user)
        query = Abstract.query.filter_by(event_id=event.id, is_deleted=False)
        if not event.can_manage(user, permission='abstracts'):
            query = query.filter((Abstract.submitter == user) |
                                 Abstract.person_links.any(AbstractPersonLink.person.has(user=user)),
                                 Abstract.state != AbstractState.invited)
        return get_page(query, Abstract, after_id, limit, lambda item: item.can_access(user),
                        lambda item: _serialize_abstract(item, user))

    @registry.tool()
    def indico_get_abstract(user, event_id: ObjectID, abstract_id: ObjectID):
        """Read an abstract you may access, including its public status, authors, and content."""
        event = _get_event(event_id, user)
        abstract = require_access(_get_abstract(event, abstract_id), user)
        if abstract.state == AbstractState.invited and not event.can_manage(user, permission='abstracts'):
            raise NotFound('Use the invitation link to access an invited abstract.')
        return _serialize_abstract(abstract, user, detail=True)

    @registry.tool(read_only=False, open_world=True)
    @no_autoflush
    def indico_submit_abstract(user, event_id: ObjectID, title: Title, description: Description | None = None,
                               authors: Authors | None = None, track_ids: TrackIDs | None = None,
                               contribution_type_id: ObjectID | None = None,
                               submission_comment: Description | None = None,
                               custom_fields: CustomFields | None = None):
        """Submit an abstract through the native call for abstracts. By default you are its primary author."""
        event = _get_event(event_id, user)
        if not event.cfa.can_submit_abstracts(user):
            raise Forbidden('You cannot submit to this call for abstracts.')
        inputs = {'title': title, 'description': description, 'authors': authors, 'track_ids': track_ids,
                  'contribution_type_id': contribution_type_id, 'submission_comment': submission_comment,
                  'custom_fields': custom_fields}
        fields, custom = _abstract_form_data(event, user,
                                             {key: value for key, value in inputs.items() if value is not None})
        abstract, sent = create_abstract(event, fields, custom, send_notifications=True)
        return dict(_serialize_abstract(abstract, user, detail=True), notifications_sent=sent)

    @registry.tool(read_only=False)
    @no_autoflush
    def indico_update_abstract(user, event_id: ObjectID, abstract_id: ObjectID, title: Title | None = None,
                               description: Description | None = None, authors: Authors | None = None,
                               track_ids: TrackIDs | None = None, contribution_type_id: ObjectID | None = None,
                               submission_comment: Description | None = None,
                               custom_fields: CustomFields | None = None):
        """Edit an abstract while the event and the abstract's current state allow editing."""
        event = _get_event(event_id, user, unlock=True)
        abstract = _get_abstract(event, abstract_id)
        if not abstract.can_edit(user):
            raise Forbidden('You are not allowed to edit this abstract.')
        inputs = {'title': title, 'description': description, 'authors': authors, 'track_ids': track_ids,
                  'contribution_type_id': contribution_type_id, 'submission_comment': submission_comment,
                  'custom_fields': custom_fields}
        if data := {key: value for key, value in inputs.items() if value is not None}:
            fields, custom = _abstract_form_data(event, user, data, abstract=abstract,
                                                 management=event.can_manage(user, permission='abstracts'))
            update_abstract(abstract, fields, custom)
        return _serialize_abstract(abstract, user, detail=True)

    @registry.tool(read_only=False, destructive=True)
    def indico_withdraw_abstract(user, event_id: ObjectID, abstract_id: ObjectID, confirm: bool):
        """Withdraw an abstract you submitted or are authorized to manage."""
        require_confirmation(confirm)
        event = _get_event(event_id, user, unlock=True)
        abstract = _get_abstract(event, abstract_id)
        if not abstract.can_withdraw(user, check_state=True):
            raise Forbidden('You are not allowed to withdraw this abstract.')
        withdraw_abstract(abstract)
        return _serialize_abstract(abstract, user)

    @registry.tool(read_only=False, destructive=True)
    def indico_delete_abstract(user, event_id: ObjectID, abstract_id: ObjectID, confirm: bool):
        """Delete an abstract from an event whose abstracts you manage. A linked contribution remains."""
        require_confirmation(confirm)
        event = _get_event(event_id, user, manage=True, unlock=True)
        abstract = _get_abstract(event, abstract_id)
        delete_abstract(abstract)
        return {'id': abstract_id, 'deleted': True}

    @registry.tool(read_only=False, idempotent=True)
    def indico_schedule_call_for_abstracts(user, event_id: ObjectID, start_dt: Timestamp,
                                           end_dt: Timestamp | None = None,
                                           modification_end_dt: Timestamp | None = None):
        """Set the submission and modification deadlines for a call for abstracts you manage."""
        event = _get_event(event_id, user, manage=True, unlock=True)
        start = parse_timestamp(start_dt)
        end = parse_timestamp(end_dt) if end_dt is not None else None
        modification_end = parse_timestamp(modification_end_dt) if modification_end_dt is not None else None
        if end is not None and end <= start:
            raise BadRequest('The submission deadline must be after the start.')
        if modification_end is not None and modification_end < (end or start):
            raise BadRequest('The modification deadline must be after submissions open and at or after they close.')
        schedule_cfa(event, start, end, modification_end)
        return _serialize_cfa(event, user)

    @registry.tool(read_only=False, idempotent=True)
    def indico_set_call_for_abstracts_open(user, event_id: ObjectID, open: bool):
        """Open or close the call for abstracts for an unlocked event whose abstracts you manage."""
        event = _get_event(event_id, user, manage=True, unlock=True)
        if open and not event.cfa.is_open:
            open_cfa(event)
        if not open and not event.cfa.has_ended:
            close_cfa(event)
        return _serialize_cfa(event, user)
