"""Run against a fresh database on INDICO_MCP_TEST_POSTGRES (a PostgreSQL maintenance DSN)."""

import pytest
from integration_support import TEST_ALL_PLUGINS, TEST_GROUP_REGISTRATION, _call, _rpc
from integration_support import pytestmark as pytestmark


def test_personal_tokens_use_native_scopes(indico_app, token):
    from indico.modules.oauth.forms import ApplicationForm, PersonalTokenForm

    for field in (ApplicationForm.allowed_scopes, PersonalTokenForm.scopes):
        assert {'read:mcp', 'write:mcp'} <= {name for name, _ in field.kwargs['choices']}

    client = indico_app.test_client()
    assert _rpc(client, None, 'tools/list').status_code == 401
    assert _rpc(client, token['unscoped'], 'tools/list').status_code == 403
    read = _rpc(client, token['read_only'], 'tools/list')
    write = _rpc(client, token['manager'], 'tools/list')
    assert read.status_code == write.status_code == 200
    assert read.headers['Cache-Control'] == 'no-store'
    read_names = {tool['name'] for tool in read.get_json()['result']['tools']}
    write_names = {tool['name'] for tool in write.get_json()['result']['tools']}
    assert 'indico_get_event' in read_names
    assert 'indico_create_event' in write_names - read_names
    response = _rpc(client, token['read_only'], 'tools/call', {'name': 'indico_create_event'})
    assert response.get_json()['error']['message'] == 'Tool unavailable'


def test_category_and_event_writes_are_persistent_and_permission_checked(indico_app, token, public_event):
    client = indico_app.test_client()
    category, event = public_event
    read_category = _call(client, token['read_only'], 'indico_get_category', category_id=category['id'])
    assert read_category['title'] == category['title']
    denied = _call(client, token['participant'], 'indico_update_event', event_id=event['id'],
                   title='Denied', error=True)
    assert denied['error'] == 'Forbidden'
    _call(client, token['manager'], 'indico_update_event', event_id=event['id'], title='Updated through MCP')
    read_back = _call(indico_app.test_client(), token['participant'], 'indico_get_event', event_id=event['id'])
    assert read_back['title'] == 'Updated through MCP'
    links = _call(client, token['read_only'], 'indico_get_event_management_links', event_id=event['id'])['links']
    assert any(link['name'] == 'registration' and link['url'].startswith('http://localhost/') for link in links)
    denied = _call(client, token['participant'], 'indico_get_event_management_links', event_id=event['id'], error=True)
    assert denied['error'] == 'Forbidden'


def _make_registration_form(client, token_value, event_id, title):
    form = _call(client, token_value, 'indico_create_registration_form', event_id=event_id, title=title)
    _call(client, token_value, 'indico_set_registration_form_open', event_id=event_id, form_id=form['id'],
          open=True, confirm=True)
    return {'event_id': event_id, 'form_id': form['id']}


def test_registration_persistence_and_participant_isolation(indico_app, token, public_event):
    client = indico_app.test_client()
    _, event = public_event
    form = _make_registration_form(client, token['manager'], event['id'], 'Participant form')
    section = _call(client, token['manager'], 'indico_create_registration_section', **form, title='Preferences')
    field = _call(client, token['manager'], 'indico_add_registration_field', **form,
                  section_id=section['id'], input_type='text', title='Dietary preferences')
    details = _call(client, token['participant'], 'indico_get_registration_form', **form)
    assert 'email' in details['parameters']
    assert field['name'] in details['parameters']
    values = {'first_name': 'Participant', 'last_name': 'Test', field['name']: 'Vegetarian'}
    _call(client, token['participant'], 'indico_submit_registration', **form,
          values={**values, 'email': 'other@example.test'}, error=True)
    registration = _call(client, token['participant'], 'indico_submit_registration', **form, values=values)
    assert registration['email'] == 'participant@example.test'
    assert registration['state'] == 'complete'
    rows = _call(indico_app.test_client(), token['manager'], 'indico_list_registrants', **form)['registrations']
    assert [row['id'] for row in rows] == [registration['id']]
    own = _call(client, token['participant'], 'indico_get_registration', **form)
    assert own['values'][field['name']] == 'Vegetarian'
    assert _call(client, token['other'], 'indico_get_registration', **form, error=True)['error'] == 'Not Found'
    assert _call(client, token['other'], 'indico_get_registration', **form, registration_id=registration['id'],
                 error=True)['error'] == 'Forbidden'


def test_registration_field_rename_move_and_empty_section_cleanup(indico_app, token, public_event):
    client = indico_app.test_client()
    manager = token['manager']
    participant = token['participant']
    form = _make_registration_form(client, manager, public_event[1]['id'], 'Market survey')
    original = _call(client, manager, 'indico_get_registration_form', **form, management=True)['form']
    affiliation = next(item for item in original['items'].values() if item['htmlName'] == 'affiliation')
    email = next(item for item in original['items'].values() if item['htmlName'] == 'email')
    questions = _call(client, manager, 'indico_create_registration_section', **form, title='Survey questions')
    old_section = _call(client, manager, 'indico_create_registration_section', **form, title='A. 基本資料')
    status = _call(client, manager, 'indico_add_registration_field', **form, section_id=questions['id'],
                   input_type='text', title='1. 目前身分')
    next_question = _call(client, manager, 'indico_add_registration_field', **form, section_id=questions['id'],
                          input_type='text', title='3. Comments')
    dropdown = _call(client, manager, 'indico_add_registration_field', **form, section_id=old_section['id'],
                     input_type='single_choice', title='2. 就讀學校', options={
                         'item_type': 'dropdown',
                         'choices': [{'id': 'new:school', 'caption': 'Example University', 'is_enabled': True}],
                     })
    _call(client, participant, 'indico_submit_registration', **form, values={
        'first_name': 'Participant', 'last_name': 'Test', 'affiliation': 'Example University',
    })
    saved = _call(client, participant, 'indico_get_registration', **form)['values']['affiliation']
    assert saved['text'] == 'Example University'

    update = dict(field_id=affiliation['id'], title='2. 就讀學校', description='目前就讀／畢業學校')
    denied = _call(client, participant, 'indico_update_registration_field', **form, **update, error=True)
    assert denied['error'] == 'Forbidden'
    renamed = _call(client, manager, 'indico_update_registration_field', **form, **update)['field']
    assert renamed['title'] == update['title'] and renamed['description'] == update['description']
    assert renamed['affiliationMode'] == affiliation['affiliationMode']
    assert renamed['htmlName'] == 'affiliation' and renamed['fieldIsPersonalData']
    required_field = _call(client, manager, 'indico_move_registration_field', **form, field_id=email['id'],
                           after_field_id=status['id'], error=True)
    assert required_field['error'] == 'Required personal data fields must stay in the built-in personal data section.'
    move = dict(field_id=affiliation['id'], after_field_id=status['id'])
    moved = _call(client, manager, 'indico_move_registration_field', **form, **move)
    assert moved['field']['sectionId'] == questions['id'] and moved['field']['position'] == 2
    assert _call(client, manager, 'indico_move_registration_field', **form, **move) == moved
    for section in (questions, old_section):
        nonempty = _call(client, manager, 'indico_delete_registration_section', **form, section_id=section['id'],
                         confirm=True, error=True)
        assert nonempty['error'] == 'Move or remove all items from this section before deleting it.'
    _call(client, manager, 'indico_delete_registration_field', **form, field_id=dropdown['id'], confirm=True)
    _call(client, manager, 'indico_delete_registration_section', **form, section_id=old_section['id'], confirm=True)

    view = _call(client, participant, 'indico_get_registration_form', **form)['form']
    assert str(old_section['id']) not in view['sections']
    assert str(dropdown['id']) not in view['items']
    question_ids = [item['id'] for item in sorted(view['items'].values(), key=lambda item: item['position'])
                    if item['sectionId'] == questions['id']]
    assert question_ids == [status['id'], affiliation['id'], next_question['id']]
    assert _call(client, participant, 'indico_get_registration', **form)['values']['affiliation'] == saved


@pytest.mark.skipif(not TEST_GROUP_REGISTRATION,
                    reason='Enable the installed Group Registration plugin to exercise its workflow')
def test_group_fields_create_a_group_through_native_registration(indico_app, token, public_event):
    from indico.vendor import django_mail

    client = indico_app.test_client()
    _, event = public_event
    form = _make_registration_form(client, token['manager'], event['id'], 'Group form')
    group_form = {'event_id': event['id'], 'reg_form_id': form['form_id']}
    settings = _call(client, token['manager'], 'indico_group_registration_update_settings', **group_form,
                     enabled=True, plans=[{'label': 'Team', 'size': 2}], confirm=True)
    info = _call(client, token['participant'], 'indico_group_registration_form', **group_form)
    field = info['group_plan_field']
    assert field and info['enabled']
    plan_id = settings['plans'][0]['id']
    _call(client, token['participant'], 'indico_submit_registration', **form,
          values={'first_name': 'Participant', 'last_name': 'Test',
                  field: {'mode': 'create', 'plan': plan_id, 'name': 'The Team', 'accepted': True}})
    group = _call(indico_app.test_client(), token['participant'], 'indico_group_registration_my_group', **group_form)
    assert group['is_leader'] and group['state'] == 'forming'
    preview = _call(client, token['manager'], 'indico_group_registration_preview_reminder', **group_form)
    assert preview['recipient_count'] == 1 and '[registration-token]' in preview['preview_body']
    before = len(django_mail.outbox)
    sent = _call(client, token['manager'], 'indico_group_registration_send_reminder', **group_form, confirm=True,
                 expected_recipient_count=preview['recipient_count'])
    assert sent['queued'] == 1
    assert len(django_mail.outbox) == before + 1 and django_mail.outbox[-1].to == ['participant@example.test']
    _call(client, token['other'], 'indico_submit_registration', **form,
          values={'first_name': 'Other', 'last_name': 'Test',
                  field: {'mode': 'join', 'code': group['join_code'], 'accepted': True}})
    confirmed = _call(client, token['manager'], 'indico_group_registration_get', **group_form, group_id=group['id'])
    assert confirmed['state'] == 'confirmed' and len(confirmed['group_members']) == 2


@pytest.mark.skipif(not TEST_ALL_PLUGINS, reason='Enable the installed STSA, Group Registration and Sentry plugins')
def test_optional_plugin_catalogs_and_permissions(indico_app, token, public_event):
    client = indico_app.test_client()
    manager = token['manager']
    _, event = public_event
    form = _make_registration_form(client, manager, event['id'], 'STSA form')
    catalog = _rpc(client, manager, 'tools/list').get_json()['result']['tools']
    assert {'indico_sentry_status', 'indico_group_registration_my_group', 'indico_stsa_registration_settings'} <= {
        tool['name'] for tool in catalog
    }
    stsa_result = _call(client, manager, 'indico_stsa_registration_settings', event_id=event['id'],
                        registration_form_id=form['form_id'])
    assert stsa_result['tables_ready'] and stsa_result['group_registration_active']
    assert len(stsa_result['forms']) == 1
    assert _call(client, manager, 'indico_sentry_status')['initialized']
    assert _call(client, token['participant'], 'indico_sentry_status', error=True)['error'] == 'Forbidden'
