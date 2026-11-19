from base64 import b64encode, urlsafe_b64encode
from hashlib import sha256
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from integration_support import _rpc, signed_in_client
from integration_support import pytestmark as pytestmark

RESOURCE = 'http://localhost/mcp'
CALLBACK = 'https://claude.ai/api/mcp/auth_callback'
REGISTER = '/mcp/oauth/register'
VERIFIER = 's' * 64
CHALLENGE = urlsafe_b64encode(sha256(VERIFIER.encode()).digest()).rstrip(b'=').decode()


def _authorize_query(client_id, **kwargs):
    return '/oauth/authorize?' + urlencode({
        'client_id': client_id, 'redirect_uri': CALLBACK, 'response_type': 'code',
        'state': 'connector-state', 'resource': RESOURCE, 'code_challenge': CHALLENGE,
        'code_challenge_method': 'S256', **kwargs,
    })


@pytest.fixture
def configure_oauth_access(indico_app, token):
    from indico.core.db import db
    from indico.core.plugins import plugin_engine

    admin, _, headers = signed_in_client(indico_app, 'Manager')
    with indico_app.app_context():
        settings = plugin_engine.get_plugin('mcp').settings
        original_access = settings.get('oauth_default_access')
        original_callbacks = settings.get('oauth_callback_urls')

    def configure(access):
        saved = admin.post('/admin/plugins/mcp/', data={
            'csrf_token': headers['X-CSRF-Token'], 'oauth_default_access': access, 'oauth_callback_urls': CALLBACK,
        })
        assert saved.status_code == 302

    try:
        yield configure
    finally:
        with indico_app.app_context():
            settings.set_multi({'oauth_default_access': original_access, 'oauth_callback_urls': original_callbacks})
            db.session.commit()


@pytest.mark.parametrize(('method', 'default_access', 'explicit_scope', 'approve_read_only'), [
    ('none', 'read-only', None, False),
    ('client_secret_basic', 'read-write', None, False),
    ('client_secret_post', 'read-only', 'read:mcp write:mcp', False),
    ('none', 'read-write', None, True),
    ('none', 'read-write', 'read:mcp', False),
])
def test_discover_register_authorize_exchange_and_call(indico_app, configure_oauth_access, method,
                                                      default_access, explicit_scope, approve_read_only):
    from lxml import html

    configure_oauth_access(default_access)
    default_scope = 'read:mcp write:mcp' if default_access == 'read-write' else 'read:mcp'
    requested_scope = explicit_scope or default_scope
    granted_scope = 'read:mcp' if approve_read_only else requested_scope
    agent = indico_app.test_client()
    rejected = _rpc(agent, None, 'tools/list')
    assert rejected.status_code == 401
    challenge = rejected.headers['WWW-Authenticate']
    assert 'resource_metadata="http://localhost/.well-known/oauth-protected-resource/mcp"' in challenge
    assert f'scope="{default_scope}"' in challenge
    resource = agent.get('/.well-known/oauth-protected-resource/mcp').get_json()
    assert resource['resource'] == RESOURCE
    assert resource['authorization_servers'] == ['http://localhost']
    assert resource['scopes_supported'] == default_scope.split()
    assert agent.get('/.well-known/oauth-protected-resource').get_json() == resource

    metadata = agent.get('/.well-known/oauth-authorization-server').get_json()
    assert metadata['registration_endpoint'] == f'http://localhost{REGISTER}'
    assert metadata['authorization_endpoint'] == 'http://localhost/oauth/authorize'
    assert metadata['token_endpoint'] == 'http://localhost/oauth/token'
    assert metadata['code_challenge_methods_supported'] == ['S256']

    client_metadata = {
        'client_name': 'Claude', 'redirect_uris': [CALLBACK], 'token_endpoint_auth_method': method,
        'grant_types': ['authorization_code', 'refresh_token'], 'response_types': ['code'],
    }
    if explicit_scope is not None:
        client_metadata['scope'] = explicit_scope
    registration = agent.post(urlsplit(metadata['registration_endpoint']).path, json=client_metadata)
    assert registration.status_code == 201, registration.get_json()
    assert registration.cache_control.no_store
    registered = registration.get_json()
    assert registered['scope'] == requested_scope
    assert registered['redirect_uris'] == [CALLBACK]
    assert registered['grant_types'] == ['authorization_code']
    assert ('client_secret' in registered) == (method != 'none')
    assert registered['token_endpoint_auth_method'] == method

    # A regular user, separate from the registering client, grants access.
    user, _, _ = signed_in_client(indico_app, 'Participant')
    options = {'scope': client_metadata['scope']} if 'scope' in client_metadata else {}
    consent = user.get(_authorize_query(registered['client_id'], **options), follow_redirects=True)
    assert consent.status_code == 200
    assert b'Connect to Indico MCP' in consent.data
    assert b'Read Indico data through MCP' in consent.data
    assert (b'Perform Indico actions through MCP' in consent.data) == ('write:mcp' in requested_scope)
    consent_path = f'{consent.request.path}?{consent.request.query_string.decode()}'
    consent_page = html.fromstring(consent.data)
    csrf, = consent_page.xpath('//input[@name="csrf_token"]/@value')
    read_only_urls = consent_page.xpath('//button[@name="confirm"]/@formaction')
    assert bool(read_only_urls) == ('write:mcp' in requested_scope)
    assert user.post(consent_path, data={'confirm': '1'}).status_code == 400
    approval_path = read_only_urls[0] if approve_read_only else consent_path
    authorized = user.post(approval_path, data={'confirm': '1', 'csrf_token': csrf})
    assert authorized.status_code == 302
    parsed = urlsplit(authorized.location)
    assert parsed._replace(query='').geturl() == CALLBACK
    params = parse_qs(parsed.query)
    assert params['state'] == ['connector-state']

    data = {
        'grant_type': 'authorization_code', 'code': params['code'][0], 'redirect_uri': CALLBACK,
        'code_verifier': VERIFIER, 'resource': RESOURCE,
    }
    headers = {}
    if method == 'client_secret_basic':
        credentials = f"{registered['client_id']}:{registered['client_secret']}".encode()
        headers = {'Authorization': f'Basic {b64encode(credentials).decode()}'}
    else:
        data['client_id'] = registered['client_id']
        if method == 'client_secret_post':
            data['client_secret'] = registered['client_secret']

    def exchange(**changes):
        return agent.post('/oauth/token', data=data | changes, headers=headers)

    assert exchange(resource='https://other.example/mcp').get_json()['error'] == 'invalid_target'
    assert exchange(code_verifier='a' * 64).status_code == 400
    exchanged = exchange()
    assert exchanged.status_code == 200, exchanged.get_json()
    assert exchange().status_code == 400
    token_response = exchanged.get_json()
    assert set(token_response['scope'].split()) == set(granted_scope.split())
    access_token = token_response['access_token']
    listing = _rpc(agent, access_token, 'tools/list')
    assert listing.status_code == 200, listing.get_json()
    tool_names = {tool['name'] for tool in listing.get_json()['result']['tools']}
    editing_tools = {'indico_update_registration_field', 'indico_move_registration_field',
                     'indico_delete_registration_field', 'indico_delete_registration_section'}
    assert tool_names & editing_tools == (editing_tools if 'write:mcp' in granted_scope else set())
    profile = _rpc(agent, access_token, 'tools/call', {'name': 'indico_get_my_profile'})
    assert profile.get_json()['result']['structuredContent']['first_name'] == 'Participant'
    assert agent.get(consent_path, headers={'Authorization': f'Bearer {access_token}'}).status_code == 403


def test_registration_rejects_unapproved_redirects_and_scope_expansion(indico_app, token):
    client = indico_app.test_client()
    unapproved = client.post(REGISTER, json={'redirect_uris': [CALLBACK + '/extra']})
    assert unapproved.status_code == 400
    assert unapproved.get_json()['error'] == 'invalid_redirect_uri'
    unscoped = client.post(REGISTER, json={'redirect_uris': [CALLBACK], 'scope': 'full:everything'})
    assert unscoped.status_code == 400
    assert unscoped.get_json()['error'] == 'invalid_client_metadata'
    registered = client.post(REGISTER, json={'redirect_uris': [CALLBACK], 'token_endpoint_auth_method': 'none'})
    assert registered.status_code == 201
    client_id = registered.get_json()['client_id']
    user, _, _ = signed_in_client(indico_app, 'Participant')
    prefix_redirect = user.get(_authorize_query(client_id, redirect_uri=CALLBACK + '/extra', scope='read:mcp'))
    assert prefix_redirect.status_code == 400
    assert prefix_redirect.get_json()['error'] == 'invalid_request'
    no_pkce = user.get(_authorize_query(client_id, code_challenge_method='', scope='read:mcp'))
    assert no_pkce.status_code == 400
    expanded = user.get(_authorize_query(client_id, scope='read:mcp full:everything'))
    assert expanded.get_json()['error'] == 'invalid_scope'


def test_callback_allowlist_controls_registration(indico_app):
    from indico.core.db import db
    from indico.core.plugins import plugin_engine

    client = indico_app.test_client()
    with indico_app.app_context():
        settings = plugin_engine.get_plugin('mcp').settings
        configured_callbacks = settings.get('oauth_callback_urls')
        settings.set('oauth_callback_urls', [])
        db.session.commit()
    try:
        metadata = client.get('/.well-known/oauth-authorization-server').get_json()
        assert 'registration_endpoint' not in metadata
        assert client.post(REGISTER, json={'redirect_uris': [CALLBACK]}).status_code == 404
    finally:
        with indico_app.app_context():
            settings.set('oauth_callback_urls', configured_callbacks)
            db.session.commit()
