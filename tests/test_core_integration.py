from datetime import datetime, timedelta

from integration_support import _call
from integration_support import pytestmark as pytestmark


def test_programme_and_material_permissions(indico_app, token, public_event):
    client = indico_app.test_client()
    manager = token['manager']
    participant = token['participant']
    category, event = public_event
    event_id = event['id']
    start = datetime.fromisoformat(event['start_dt'])

    protected = _call(client, manager, 'indico_create_event', category_id=category['id'], title='Protected',
                      event_type='conference', start_dt=event['start_dt'], end_dt=event['end_dt'],
                      protection='protected')
    assert _call(client, participant, 'indico_get_event', event_id=protected['id'], error=True)['error'] == 'Forbidden'
    events = _call(client, participant, 'indico_list_events', category_id=category['id'])['items']
    assert {item['id'] for item in events} == {event_id}

    contribution = _call(client, manager, 'indico_create_contribution', event_id=event_id, title='Talk')
    event_session = _call(client, manager, 'indico_create_session', event_id=event_id, title='Session')
    block = _call(client, manager, 'indico_create_session_block', event_id=event_id, session_id=event_session['id'],
                  start_dt=start.isoformat(), duration_minutes=60)
    _call(client, manager, 'indico_schedule_contribution', event_id=event_id, contribution_id=contribution['id'],
          start_dt=start.isoformat(), session_block_id=block['object_id'])
    _call(client, manager, 'indico_set_contributions_published', event_id=event_id, published=True)
    updated_block = _call(client, manager, 'indico_update_session_block', event_id=event_id,
                          block_id=block['object_id'], start_dt=(start + timedelta(minutes=5)).isoformat())
    assert _call(client, participant, 'indico_get_contribution', event_id=event_id,
                 contribution_id=contribution['id'])['start_dt'] == updated_block['start_dt']

    _call(client, manager, 'indico_set_event_locked', event_id=event_id, locked=True)
    assert _call(client, manager, 'indico_update_contribution', event_id=event_id, contribution_id=contribution['id'],
                 title='Locked', error=True)['error'] == 'Forbidden'
    _call(client, manager, 'indico_set_event_locked', event_id=event_id, locked=False)

    target = {'id': event_id, 'kind': 'event'}
    folder = _call(client, manager, 'indico_create_material_folder', target=target, title='Resources')
    _call(client, manager, 'indico_add_material_link', target=target, title='Material page',
          url='https://example.com/material', folder_id=folder['id'])
    assert _call(client, participant, 'indico_list_materials', target=target)['items'][0]['title'] == 'Material page'


def test_abstract_submission_honors_default_speaker_requirement(indico_app, token, public_event):
    client = indico_app.test_client()
    manager = token['manager']
    participant = token['participant']
    event_id = public_event[1]['id']

    _call(client, manager, 'indico_set_event_feature', event_id=event_id, name='abstracts', enabled=True)
    _call(client, manager, 'indico_set_call_for_abstracts_open', event_id=event_id, open=True)
    abstract = _call(client, participant, 'indico_submit_abstract', event_id=event_id, title='New abstract',
                     description='Test Content')
    assert abstract['authors'][0]['role'] == 'primary' and abstract['authors'][0]['speaker']
    updated = _call(client, participant, 'indico_update_abstract', event_id=event_id, abstract_id=abstract['id'],
                    title='Updated abstract')
    assert updated['title'] == 'Updated abstract'


def test_survey_draft_to_anonymous_submission(indico_app, token, public_event):
    client = indico_app.test_client()
    manager = token['manager']
    participant = token['participant']
    event_id = public_event[1]['id']

    _call(client, manager, 'indico_set_event_feature', event_id=event_id, name='surveys', enabled=True)
    survey = _call(client, manager, 'indico_create_survey', event_id=event_id, title='Feedback', anonymous=True,
                   partial_completion=True)
    form = {'event_id': event_id, 'survey_id': survey['id']}
    question = _call(client, manager, 'indico_add_survey_question', **form, section_id=survey['section_id'],
                     field_type='text', title='Feedback', required=True)
    _call(client, manager, 'indico_set_survey_open', **form, open=True)

    answers = {question['input_name']: 'Great!'}
    _call(client, participant, 'indico_save_survey_draft', **form, answers=answers)
    questionnaire = _call(client, participant, 'indico_get_survey', **form)
    assert questionnaire['pending_answers'] == {question['input_name']: ['Great!']}
    _call(client, participant, 'indico_submit_survey', **form, answers=answers)
    _call(client, participant, 'indico_submit_survey', **form, answers=answers, error=True)
    responses = _call(client, manager, 'indico_list_survey_submissions', **form)['items']
    assert len(responses) == 1 and responses[0]['user_id'] is None


def test_survey_choice_questions_take_plain_options_and_reject_malformed_config(indico_app, token, public_event):
    client = indico_app.test_client()
    manager = token['manager']
    event_id = public_event[1]['id']

    _call(client, manager, 'indico_set_event_feature', event_id=event_id, name='surveys', enabled=True)
    survey = _call(client, manager, 'indico_create_survey', event_id=event_id, title='Choices')
    question = {'event_id': event_id, 'survey_id': survey['id'], 'section_id': survey['section_id'],
                'field_type': 'single_choice', 'title': 'Attending?'}

    plain = _call(client, manager, 'indico_add_survey_question', **question, config={
        'display_type': 'select', 'options': ['Yes', 'No'],
    })
    assert [option['option'] for option in plain['config']['options']] == ['Yes', 'No']
    assert len({option['id'] for option in plain['config']['options']}) == 2
    native = _call(client, manager, 'indico_add_survey_question', **question, config={
        'display_type': 'select', 'options': '[{"option": "Maybe"}, {"option": "Later"}]',
    })
    assert [option['option'] for option in native['config']['options']] == ['Maybe', 'Later']

    for options in ([], ['Yes', 'Yes'], [''], '[{"label": "Yes"}]', '[1, 2]'):
        rejected = _call(client, manager, 'indico_add_survey_question', **question, error=True,
                         config={'display_type': 'select', 'options': options})
        assert 'options' in rejected['error']
    missing = _call(client, manager, 'indico_add_survey_question', **question, error=True,
                    config={'display_type': 'select'})
    assert 'options' in missing['error']
    unknown = _call(client, manager, 'indico_add_survey_question', **question, error=True, config={'choices': 'Yes'})
    assert unknown['error'] == 'One or more configuration keys are unavailable for this question type.'
    survey_questions = _call(client, manager, 'indico_get_survey', event_id=event_id, survey_id=survey['id'],
                             management=True)['sections'][0]['items']
    assert [item['id'] for item in survey_questions] == [plain['id'], native['id']]
