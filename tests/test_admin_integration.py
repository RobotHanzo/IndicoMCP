from hashlib import sha256
from json import loads
from uuid import uuid4

from integration_support import _rpc
from integration_support import pytestmark as pytestmark

SETUP_PAGE = '/admin/plugins/mcp/'
CREATE_TOKEN = f'{SETUP_PAGE}token'


def _signed_in_client(app, user_name):
    from indico.modules.users import User

    client = app.test_client()
    csrf = str(uuid4())
    with app.app_context():
        user = User.query.filter_by(first_name=user_name).one()
        user_id = user.id
        with client.session_transaction() as browser_session:
            browser_session.set_session_user(user)
            browser_session['_csrf_token'] = csrf
    return client, user_id, {'X-CSRF-Token': csrf, 'X-Requested-With': 'XMLHttpRequest'}


def test_admin_page_issues_tokens_that_work_at_mcp_endpoint(indico_app, token):
    from indico.core.oauth.models.personal_tokens import PersonalToken
    from lxml import html

    client, user_id, headers = _signed_in_client(indico_app, 'Manager')
    response = client.get(SETUP_PAGE)
    assert response.status_code == 200
    page = html.fromstring(response.data)
    assert page.xpath('//input[@readonly]/@value') == ['http://localhost/mcp']
    assert 'Streamable HTTP' in page.text_content()
    assert page.xpath('//a[@href="/user/tokens/"]')
    buttons = page.xpath('//button[@data-href=$url][@data-method="POST"][@data-ajax-dialog]', url=CREATE_TOKEN)
    assert [loads(button.get('data-params')) for button in buttons] == [
        {'access': 'read-only'}, {'access': 'read-write'},
    ]

    for button, scopes in zip(buttons, [{'read:mcp'}, {'read:mcp', 'write:mcp'}], strict=True):
        created = client.post(CREATE_TOKEN, data=loads(button.get('data-params')), headers=headers)
        assert created.status_code == 200
        assert created.cache_control.no_store
        dialog = html.fromstring(created.get_json()['html'])
        access_token, = dialog.xpath('//input[@readonly]/@value')
        assert access_token.startswith('indp_')
        assert dialog.xpath('//button[@data-clipboard-target]')

        with indico_app.app_context():
            token_hash = sha256(access_token.encode()).hexdigest()
            stored = PersonalToken.query.filter_by(access_token_hash=token_hash).one()
            assert stored.user_id == user_id
            assert stored.scopes == scopes
            assert stored.name.startswith('Indico MCP (')

        listing = _rpc(indico_app.test_client(), access_token, 'tools/list')
        assert listing.status_code == 200
        tools = {tool['name'] for tool in listing.get_json()['result']['tools']}
        assert 'indico_get_event' in tools
        assert ('indico_create_event' in tools) == ('write:mcp' in scopes)
        assert access_token.encode() not in client.get(SETUP_PAGE).data
        with client.session_transaction() as browser_session:
            assert access_token not in repr(dict(browser_session))


def test_token_creation_requires_admin_browser_session_and_csrf(indico_app, token):
    from indico.core.db import db
    from indico.core.oauth.models.personal_tokens import PersonalToken
    from indico.modules.users import User

    admin, _, headers = _signed_in_client(indico_app, 'Manager')
    participant, _, participant_headers = _signed_in_client(indico_app, 'Participant')
    data = {'access': 'read-write'}
    with indico_app.app_context():
        owner = User.query.filter_by(first_name='Manager').one()
        bearer = PersonalToken(user=owner, name='Admin bearer for API test', scopes=['full:everything'])
        raw_bearer = bearer.generate_token()
        db.session.add(bearer)
        db.session.commit()
        total_tokens = PersonalToken.query.count()

    assert admin.get(CREATE_TOKEN).status_code == 405
    assert admin.post(CREATE_TOKEN, data=data).status_code == 400
    assert admin.post(CREATE_TOKEN, data={'access': 'full:everything'}, headers=headers).status_code == 400
    assert participant.get(SETUP_PAGE).status_code == 403
    assert participant.post(CREATE_TOKEN, data=data, headers=participant_headers).status_code == 403
    assert indico_app.test_client().post(CREATE_TOKEN, data=data, headers={
        'Authorization': f'Bearer {raw_bearer}', 'X-Requested-With': 'XMLHttpRequest',
    }).status_code == 403

    with indico_app.app_context():
        assert PersonalToken.query.count() == total_tokens
