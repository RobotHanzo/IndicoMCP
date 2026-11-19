"""Tools for creating, managing, and answering event surveys."""

from secrets import compare_digest
from typing import Annotated

from indico.core.db import db
from indico.core.db.sqlalchemy.util.session import no_autoflush
from indico.modules.events.surveys import logger
from indico.modules.events.surveys.fields import get_field_types
from indico.modules.events.surveys.models.anonymous_submissions import AnonymousSurveySubmission
from indico.modules.events.surveys.models.items import SurveyItemType, SurveySection
from indico.modules.events.surveys.models.submissions import SurveyAnswer, SurveySubmission
from indico.modules.events.surveys.models.surveys import Survey, SurveyState
from indico.modules.events.surveys.operations import add_survey_question, add_survey_section
from indico.modules.events.surveys.util import make_survey_form, query_active_surveys, was_survey_submitted
from indico.util.date_time import now_utc
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
    parse_timestamp,
    require_access,
    require_confirmation,
    require_manage,
    require_unlocked,
)
from indico_mcp.core_schedule import CustomFields

SurveyToken = Annotated[str, Field(min_length=36, max_length=36, pattern=r'^[0-9a-fA-F-]+$')]
SurveyCapacity = Annotated[int, Field(ge=0, le=2147483647)]
ACTIVE_STATES = {SurveyState.active_and_clean, SurveyState.active_and_answered}
SCHEDULABLE_STATES = {SurveyState.ready_to_open, SurveyState.active_and_clean, SurveyState.finished}


def _get_event(event_id, user, *, manage=False, unlock=False):
    event = get_event(event_id)
    if manage:
        require_manage(event, user, permission='surveys')
    else:
        require_access(event, user)
    if not event.has_feature('surveys'):
        raise NotFound('Surveys are disabled for this event.')
    if unlock:
        require_unlocked(event)
    return event


def _get_survey(event, survey_id, *, lock=False):
    if lock:
        # Indico allocates friendly submission IDs using a separate database
        # transaction that updates the survey row. A row lock would block it.
        key = db.func.hashtext('indico_mcp.survey')
        db.session.execute(db.select([db.func.pg_advisory_xact_lock(key, survey_id)]))
    return Survey.query.filter_by(event_id=event.id, id=survey_id, is_deleted=False).first_or_404()


def _serialize_survey(survey, *, management=False):
    endpoint = 'surveys.manage_survey' if management else 'surveys.display_survey_form'
    return {'id': survey.id, 'title': survey.title, 'introduction_markdown': survey.introduction,
            'url': url_for(endpoint, survey, _external=True), 'state': survey.state.name,
            'private': survey.private, 'anonymous': survey.anonymous, 'require_user': survey.require_user,
            'partial_completion': survey.partial_completion, 'submission_limit': survey.submission_limit,
            'start_dt': survey.start_dt.isoformat() if survey.start_dt else None,
            'end_dt': survey.end_dt.isoformat() if survey.end_dt else None}


def _question_data(item):
    return {'id': item.id, 'type': item.field_type, 'input_name': f'question_{item.id}', 'title': item.title,
            'required': item.is_required, 'description': str(item.description), 'config': item.field_data}


def _section_data(section):
    return {'id': section.id, 'title': section.title, 'description': str(section.description),
            'display_as_section': section.display_as_section,
            'items': [_question_data(item) if item.type == SurveyItemType.question
                      else {'id': item.id, 'type': 'text', 'description': str(item.description)}
                      for item in section.children]}


def _pending_submission(survey, user, token):
    if not survey.is_visible:
        raise NotFound('The survey is unavailable.')
    pending = user.survey_submissions.filter_by(survey=survey, is_submitted=False).first()
    if survey.private and pending is None and not (token and compare_digest(token, str(survey.uuid))):
        raise NotFound('The survey is unavailable.')
    return pending


def _answer_form(survey, answers, *, partial=False):
    form = make_survey_form(survey)(formdata=MultiDict(answers), csrf_enabled=False)
    names = {f'question_{question.id}' for question in survey.questions}
    if any(name not in form for name in names):
        raise BadRequest('This survey uses a question type that is unavailable.')
    if not answers.keys() <= names:
        raise BadRequest('Use input_name values from the survey questionnaire.')
    if not partial and not form.validate():
        raise BadRequest('Invalid survey answers: ' + ', '.join(sorted(form.errors)))
    return form


@no_autoflush
def _save_submission(survey, user, pending, form):
    submission = pending if pending is not None else SurveySubmission(survey=survey, user=user)
    for question in survey.questions:
        submission.answers.append(SurveyAnswer(question=question, data=form[f'question_{question.id}'].data))
    submission.is_anonymous = survey.anonymous
    if survey.anonymous:
        submission.user = None
        survey.anonymous_submissions.append(AnonymousSurveySubmission(user=user))
    submission.submitted_dt = now_utc()
    submission.is_submitted = True
    submission.pending_answers = {}
    db.session.flush()
    return submission


def _validate_title(event, title, survey_id=None):
    query = Survey.query.filter(Survey.event_id == event.id, ~Survey.is_deleted,
                                db.func.lower(Survey.title) == title.lower())
    if survey_id is not None:
        query = query.filter(Survey.id != survey_id)
    if query.has_rows():
        raise BadRequest('There is already a survey with this title in the event.')


def register_tools(registry):
    @registry.tool()
    def indico_list_surveys(user, event_id: ObjectID, management: bool = False,
                            after_id: CategoryID = 0, limit: PageSize = 30):
        """List active surveys visible to you, or all surveys for an event whose surveys you manage."""
        event = _get_event(event_id, user, manage=management)
        query = (Survey.query.filter_by(event_id=event.id, is_deleted=False)
                 if management else query_active_surveys(event))
        return get_page(query, Survey, after_id, limit, lambda _: True,
                        lambda survey: _serialize_survey(survey, management=management))

    @registry.tool()
    def indico_get_survey(user, event_id: ObjectID, survey_id: ObjectID, token: SurveyToken | None = None,
                          management: bool = False):
        """Read a questionnaire and your draft answers. Private surveys require their invitation token or your draft."""
        event = _get_event(event_id, user, manage=management)
        survey = _get_survey(event, survey_id)
        result = _serialize_survey(survey, management=management)
        if not management:
            pending = _pending_submission(survey, user, token)
            result.update(submitted=was_survey_submitted(survey),
                          pending_answers=pending.pending_answers if pending is not None else {})
        result['sections'] = [_section_data(section) for section in survey.sections]
        return result

    @registry.tool(read_only=False, open_world=True)
    def indico_submit_survey(user, event_id: ObjectID, survey_id: ObjectID, answers: CustomFields,
                             token: SurveyToken | None = None):
        """Submit responses using questionnaire input_name values. This finalizes the survey response."""
        event = _get_event(event_id, user)
        survey = _get_survey(event, survey_id, lock=True)
        pending = _pending_submission(survey, user, token)
        if not survey.can_submit(user) or was_survey_submitted(survey):
            raise Forbidden('You cannot submit to this survey.')
        submission = _save_submission(survey, user, pending, _answer_form(survey, answers))
        survey.send_submission_notification(submission)
        return {'survey_id': survey.id, 'submitted': True, 'anonymous': survey.anonymous}

    @registry.tool(read_only=False, idempotent=True)
    def indico_save_survey_draft(user, event_id: ObjectID, survey_id: ObjectID, answers: CustomFields,
                                 token: SurveyToken | None = None):
        """Save a complete replacement draft for a survey that supports partial completion."""
        event = _get_event(event_id, user)
        survey = _get_survey(event, survey_id, lock=True)
        pending = _pending_submission(survey, user, token)
        if not survey.can_submit(user) or not survey.partial_completion or was_survey_submitted(survey):
            raise Forbidden('You cannot save a draft for this survey.')
        _answer_form(survey, answers, partial=True)
        if pending is None:
            pending = SurveySubmission(survey=survey, user=user)
        pending.is_anonymous = survey.anonymous
        pending.pending_answers = {key: value if isinstance(value, list) else [value] for key, value in answers.items()}
        return {'survey_id': survey.id, 'saved': True}

    @registry.tool(read_only=False)
    def indico_create_survey(user, event_id: ObjectID, title: Title, introduction: Description = '',
                             anonymous: bool = False, require_user: bool = True, private: bool = False,
                             partial_completion: bool = False, submission_limit: SurveyCapacity = 0):
        """Create a survey and an initial question section. Zero means unlimited submissions."""
        event = _get_event(event_id, user, manage=True, unlock=True)
        _validate_title(event, title)
        survey = Survey(event=event, title=title, introduction=introduction, anonymous=anonymous, private=private,
                        require_user=require_user or not anonymous, partial_completion=partial_completion,
                        submission_limit=submission_limit or None)
        survey.items.append(SurveySection(display_as_section=False))
        db.session.add(survey)
        db.session.flush()
        logger.info('Survey %s created by %s', survey, user)
        return dict(_serialize_survey(survey, management=True), section_id=survey.items[0].id)

    @registry.tool(read_only=False, idempotent=True)
    def indico_update_survey(user, event_id: ObjectID, survey_id: ObjectID, title: Title | None = None,
                             introduction: Description | None = None, anonymous: bool | None = None,
                             require_user: bool | None = None, private: bool | None = None,
                             partial_completion: bool | None = None, submission_limit: SurveyCapacity | None = None):
        """Edit survey settings. Zero clears the submission limit and identified surveys always require login."""
        event = _get_event(event_id, user, manage=True, unlock=True)
        survey = _get_survey(event, survey_id, lock=True)
        if title is not None:
            _validate_title(event, title, survey.id)
            survey.title = title
        fields = {'introduction': introduction, 'anonymous': anonymous, 'require_user': require_user,
                  'private': private, 'partial_completion': partial_completion}
        for key, value in fields.items():
            if value is not None:
                setattr(survey, key, value)
        if not survey.anonymous:
            survey.require_user = True
        if submission_limit is not None:
            survey.submission_limit = submission_limit or None
        logger.info('Survey %s modified by %s', survey, user)
        return _serialize_survey(survey, management=True)

    @registry.tool()
    def indico_list_survey_question_types(user, event_id: ObjectID):
        """List native question types that a survey manager can add."""
        _get_event(event_id, user, manage=True)
        items = []
        for name, field in sorted(get_field_types().items()):
            form = field.create_config_form(formdata=None, csrf_enabled=False)
            configuration = [
                {'name': entry.name, 'type': entry.type, 'title': str(entry.label.text),
                 'required': bool(entry.flags.required), 'description': str(entry.description)}
                for entry in form if entry.name not in {'title', 'is_required', 'description'}
            ]
            items.append({'type': name, 'title': str(field.friendly_name), 'config_fields': configuration})
        return {'items': items}

    @registry.tool(read_only=False)
    def indico_add_survey_section(user, event_id: ObjectID, survey_id: ObjectID, title: Title,
                                  description: Description = ''):
        """Add a titled section to a survey you manage."""
        event = _get_event(event_id, user, manage=True, unlock=True)
        survey = _get_survey(event, survey_id, lock=True)
        return _section_data(add_survey_section(survey, {'title': title, 'description': description,
                                                         'display_as_section': True}))

    @registry.tool(read_only=False)
    def indico_add_survey_question(user, event_id: ObjectID, survey_id: ObjectID, section_id: ObjectID,
                                   field_type: ShortText, title: Title, required: bool = False,
                                   description: Description = '', config: CustomFields | None = None):
        """Add a question using the selected native question type and its form configuration fields."""
        event = _get_event(event_id, user, manage=True, unlock=True)
        survey = _get_survey(event, survey_id, lock=True)
        section = SurveySection.query.filter_by(survey_id=survey.id, id=section_id).first_or_404()
        field = get_field_types().get(field_type)
        if field is None:
            raise BadRequest('Choose a native question type from indico_list_survey_question_types.')
        options = config or {}
        inputs = {**options, 'title': title, 'is_required': 'y' if required else '', 'description': description}
        form = field.create_config_form(formdata=MultiDict(inputs), csrf_enabled=False)
        if any(name not in form for name in inputs):
            raise BadRequest('One or more configuration keys are unavailable for this question type.')
        if not form.validate():
            raise BadRequest('Invalid survey question configuration: ' + ', '.join(sorted(form.errors)))
        return _question_data(add_survey_question(section, field, form.data))

    @registry.tool(read_only=False, idempotent=True)
    def indico_schedule_survey(user, event_id: ObjectID, survey_id: ObjectID,
                               start_dt: Timestamp | None = None, end_dt: Timestamp | None = None):
        """Schedule an existing survey. An omitted end clears its closing time. Omit start to preserve it."""
        event = _get_event(event_id, user, manage=True, unlock=True)
        survey = _get_survey(event, survey_id, lock=True)
        if start_dt is not None and survey.state not in SCHEDULABLE_STATES:
            raise BadRequest('The opening time cannot change in the survey’s current state.')
        start = parse_timestamp(start_dt) if start_dt is not None else survey.start_dt
        end = parse_timestamp(end_dt) if end_dt is not None else None
        if start is not None and end is not None and end < start:
            raise BadRequest('The closing time must be at or after the opening time.')
        survey.start_dt = start
        survey.end_dt = end
        logger.info('Survey %s scheduled by %s', survey, user)
        return _serialize_survey(survey, management=True)

    @registry.tool(read_only=False, idempotent=True, open_world=True)
    def indico_set_survey_open(user, event_id: ObjectID, survey_id: ObjectID, open: bool):
        """Open, reopen, or close a survey. Opening sends any configured notifications."""
        event = _get_event(event_id, user, manage=True, unlock=True)
        survey = _get_survey(event, survey_id, lock=True)
        state = survey.state
        if open and state not in ACTIVE_STATES:
            if state == SurveyState.finished:
                survey.end_dt = None
                survey.start_notification_sent = False
            elif state == SurveyState.ready_to_open:
                survey.open()
            else:
                raise BadRequest('This survey cannot be opened in its current state.')
            survey.send_start_notification()
            logger.info('Survey %s opened by %s', survey, user)
        elif not open and state in ACTIVE_STATES | {SurveyState.limit_reached} and not survey.has_ended:
            survey.close()
            logger.info('Survey %s closed by %s', survey, user)
        return _serialize_survey(survey, management=True)

    @registry.tool()
    def indico_list_survey_submissions(user, event_id: ObjectID, survey_id: ObjectID,
                                       after_id: CategoryID = 0, limit: PageSize = 30):
        """Read completed responses to a survey you manage. Anonymous responses have no user ID."""
        event = _get_event(event_id, user, manage=True)
        survey = _get_survey(event, survey_id)
        query = SurveySubmission.query.filter_by(survey_id=survey.id, is_submitted=True)

        def serialize(submission):
            return {'id': submission.id, 'submitted_dt': submission.submitted_dt.isoformat(),
                    'user_id': None if submission.is_anonymous else submission.user_id,
                    'answers': {f'question_{answer.question_id}': answer.data for answer in submission.answers}}

        return get_page(query, SurveySubmission, after_id, limit, lambda _: True, serialize)

    @registry.tool(read_only=False, destructive=True)
    def indico_delete_survey(user, event_id: ObjectID, survey_id: ObjectID, confirm: bool):
        """Delete a survey and remove its responses from the management interface."""
        require_confirmation(confirm)
        event = _get_event(event_id, user, manage=True, unlock=True)
        survey = _get_survey(event, survey_id, lock=True)
        survey.is_deleted = True
        logger.info('Survey %s deleted by %s', survey, user)
        return {'id': survey.id, 'deleted': True}
