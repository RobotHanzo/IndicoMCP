from urllib.parse import urlsplit
from uuid import UUID, uuid4

from authlib.oauth2 import OAuth2Error
from authlib.oauth2.rfc6749.util import extract_basic_authorization
from flask import current_app, jsonify, make_response, redirect, request, session
from indico.core.config import config
from indico.core.db import db
from indico.core.limiter import make_rate_limiter
from indico.core.oauth.models.applications import OAuthApplication
from indico.core.oauth.oauth2 import auth_server
from indico.core.oauth.scopes import SCOPES
from indico.core.plugins import plugin_engine, render_plugin_template, url_for_plugin
from indico.util.i18n import _
from indico.web.flask.util import url_for
from indico.web.rh import RH, cors, custom_auth, json_errors
from indico.web.util import get_request_user
from werkzeug.exceptions import Forbidden, RequestEntityTooLarge

from indico_mcp.models import OAuthClientRegistration
from indico_mcp.plugin import ACCESS_SCOPES, READ_SCOPE, WRITE_SCOPE

MCP_SCOPES = frozenset({READ_SCOPE, WRITE_SCOPE})
AUTH_METHODS = {'none', 'client_secret_basic', 'client_secret_post'}
AUTHORIZATION_PARAMETERS = {'client_id', 'redirect_uri', 'response_type', 'state', 'resource',
                            'code_challenge', 'code_challenge_method'}
registration_rate = make_rate_limiter('mcp-client-registration', '10/minute')
site_registration_rate = make_rate_limiter('mcp-client-registration-site', '100/minute', by_ip=False)


def mcp_url():
    return url_for_plugin('mcp.mcp_endpoint', _external=True)


def _callback_urls():
    return plugin_engine.get_plugin('mcp').settings.get('oauth_callback_urls')


def _default_oauth_scopes():
    return ACCESS_SCOPES[plugin_engine.get_plugin('mcp').settings.get('oauth_default_access')]


def _registration_table_exists():
    query = db.text("SELECT to_regclass('plugin_mcp.oauth_client_registrations')")
    return db.session.execute(query).scalar() is not None


def automatic_registration_available():
    return bool(_callback_urls()) and _registration_table_exists()


def add_registration_endpoint(sender, result, **kwargs):
    if automatic_registration_available():
        metadata = result.get_json()
        metadata['registration_endpoint'] = url_for_plugin('mcp.register_client', _external=True)
        result.set_data(current_app.json.dumps(metadata))


def _discovery_scopes():
    scopes = _default_oauth_scopes()
    if WRITE_SCOPE in scopes and not automatic_registration_available():
        return (READ_SCOPE,)
    return scopes


def add_discovery_challenge(response):
    header = response.headers.get('WWW-Authenticate', 'Bearer')
    if header.lower().split(' ', 1)[0] != 'bearer':
        return
    metadata_url = url_for_plugin('mcp.protected_resource_metadata', _external=True)
    separator = ' ' if header.strip().lower() == 'bearer' else ', '
    scopes = ' '.join(_discovery_scopes())
    challenge = f'resource_metadata="{metadata_url}", scope="{scopes}"'
    response.headers['WWW-Authenticate'] = f'{header}{separator}{challenge}'


def _oauth_error(error, description, status=400):
    return make_response(jsonify(error=error, error_description=str(description)), status)


@cors
@custom_auth
class RHProtectedResourceMetadata(RH):
    def _process_GET(self):
        return jsonify(resource=mcp_url(), authorization_servers=[config.BASE_URL],
                       bearer_methods_supported=['header'], scopes_supported=list(_discovery_scopes()))


def _registration_metadata(data, allowed_urls):
    redirect_uris = data.get('redirect_uris')
    if (not isinstance(redirect_uris, list) or not 1 <= len(redirect_uris) <= 10
            or any(not isinstance(uri, str) or uri not in allowed_urls for uri in redirect_uris)):
        return None, _oauth_error('invalid_redirect_uri', _('Every callback URL must be approved in Indico MCP.'))

    auth_method = data.get('token_endpoint_auth_method', 'client_secret_basic')
    grants = data.get('grant_types', ['authorization_code'])
    response_types = data.get('response_types', ['code'])
    if (not isinstance(auth_method, str) or auth_method not in AUTH_METHODS
            or not isinstance(grants, list) or 'authorization_code' not in grants
            or any(grant not in ('authorization_code', 'refresh_token') for grant in grants)
            or response_types != ['code']):
        return None, _oauth_error('invalid_client_metadata', _('Use the OAuth authorization code flow with PKCE.'))

    scope = data.get('scope', ' '.join(_default_oauth_scopes()))
    if not isinstance(scope, str) or not set(scope.split()) <= MCP_SCOPES:
        return None, _oauth_error('invalid_client_metadata', _('Only MCP scopes can be registered.'))

    return {
        'redirect_uris': list(dict.fromkeys(redirect_uris)),
        'token_endpoint_auth_method': auth_method,
        'grant_types': ['authorization_code'],
        'response_types': ['code'],
        'scope': ' '.join(sorted({READ_SCOPE, *scope.split()})),
    }, None


@cors
@custom_auth
@json_errors
class RHRegisterOAuthClient(RH):
    CSRF_ENABLED = False

    def _process_POST(self):
        allowed_urls = _callback_urls()
        if not allowed_urls or not _registration_table_exists():
            return _oauth_error('registration_not_supported', _('Automatic client registration is unavailable.'), 404)

        request.max_content_length = 8192
        try:
            data = request.get_json(silent=True)
        except RequestEntityTooLarge:
            return _oauth_error('invalid_client_metadata', _('Client metadata is too large.'), 413)
        except RecursionError:
            data = None
        if not isinstance(data, dict):
            return _oauth_error('invalid_client_metadata', _('Send client metadata as a JSON object.'))
        metadata, error = _registration_metadata(data, allowed_urls)
        if error is not None:
            return error
        if not registration_rate.hit() or not site_registration_rate.hit():
            return _oauth_error('temporarily_unavailable', _('Too many client registrations. Try again later.'), 429)

        client_id = str(uuid4())
        host = urlsplit(metadata['redirect_uris'][0]).hostname
        auth_method = metadata['token_endpoint_auth_method']
        application = OAuthApplication(
            client_id=client_id,
            name=f'MCP connector ({host}) {client_id}',
            description='Automatically registered by Indico MCP.',
            redirect_uris=metadata['redirect_uris'],
            allowed_scopes=metadata['scope'].split(),
            allow_pkce_flow=auth_method == 'none',
            is_trusted=False,
        )
        registration = OAuthClientRegistration(application=application, auth_method=auth_method)
        db.session.add(registration)
        db.session.flush()
        metadata.update(client_id=application.client_id, client_name=application.name,
                        client_id_issued_at=int(registration.created_dt.timestamp()))
        if auth_method != 'none':
            metadata.update(client_secret=application.client_secret, client_secret_expires_at=0)
        return jsonify(metadata), 201


def _registered_client(client_ids):
    valid_ids = set()
    for client_id in client_ids:
        try:
            valid_ids.add(str(UUID(client_id)))
        except (ValueError, TypeError):
            continue
    if not valid_ids or not _registration_table_exists():
        return None
    return (OAuthClientRegistration.query.join(OAuthClientRegistration.application)
            .filter(OAuthApplication.client_id.in_(valid_ids)).first())


def _is_resource(values):
    resources = values.getlist('resource')
    # The token can only be used at one MCP resource. Infer it for older clients.
    return not resources or resources == [mcp_url()]


def _is_callback(application, values):
    redirects = values.getlist('redirect_uri')
    return len(redirects) == 1 and redirects[0] in application.redirect_uris and redirects[0] in _callback_urls()


def _authorization_url(values, scope):
    args = {key: value for key, value in values.items() if key in AUTHORIZATION_PARAMETERS}
    return url_for('oauth.oauth_authorize', **args, scope=scope)


def authorize_registered_client(sender, rh, **kwargs):
    values = request.values
    registration = _registered_client(values.getlist('client_id'))
    if registration is None:
        return None

    if get_request_user()[1] != 'session':
        raise Forbidden
    if request.method == 'POST':
        rh.CSRF_ENABLED = True
        rh._check_csrf()

    app = registration.application
    if len(values.getlist('client_id')) != 1 or not _is_callback(app, values):
        return _oauth_error('invalid_request', _('Use the exact registered callback URL.'))
    if not _is_resource(values):
        return _oauth_error('invalid_target', _('The resource must be this Indico MCP server.'))
    if (values.getlist('code_challenge_method') != ['S256'] or len(values.getlist('code_challenge')) != 1
            or not values['code_challenge']):
        return _oauth_error('invalid_request', _('A PKCE S256 challenge is required.'))

    if request.method == 'GET' and values.getlist('scope') in ([], ['']):
        # Keep the default within the scopes registered for this client.
        default_scope = ' '.join(scope for scope in _default_oauth_scopes() if scope in app.allowed_scopes)
        return redirect(_authorization_url(values, default_scope or READ_SCOPE))
    scope = set(values.get('scope', '').split())
    if len(values.getlist('scope')) != 1 or READ_SCOPE not in scope or not scope <= MCP_SCOPES:
        return _oauth_error('invalid_scope', _('Request read:mcp and, optionally, write:mcp.'))

    try:
        grant = auth_server.get_consent_grant(end_user=session.user)
    except OAuth2Error as error:
        return _oauth_error(error.error, error.description)
    if request.method == 'POST':
        user = session.user if 'confirm' in request.form else None
        return auth_server.create_authorization_response(grant_user=user, grant=grant)
    read_only_url = _authorization_url(values, READ_SCOPE) if WRITE_SCOPE in scope else None
    return render_plugin_template('oauth_consent.html', application=app, scopes=[SCOPES[s] for s in sorted(scope)],
                                  read_only_url=read_only_url)


def validate_registered_client_token_request(sender, **kwargs):
    try:
        basic_id, _secret = extract_basic_authorization(request.headers)
    except (ValueError, TypeError):
        return _oauth_error('invalid_client', _('Invalid client authentication.'))
    client_ids = request.values.getlist('client_id')
    registration = _registered_client([*client_ids, basic_id])
    if registration is None:
        return None

    from indico_mcp.endpoint import _protect_request_trace

    _protect_request_trace()
    app = registration.application
    values = request.form
    if request.query_string:
        return _oauth_error('invalid_request', _('Send token request parameters in the form body.'))
    method = 'client_secret_basic' if basic_id else ('client_secret_post' if 'client_secret' in values else 'none')
    if (registration.auth_method != method or (basic_id and client_ids) or
            (not basic_id and client_ids != [app.client_id]) or len(values.getlist('client_secret')) > 1):
        return _oauth_error('invalid_client', _('Use the registered client authentication method.'))
    if values.getlist('grant_type') != ['authorization_code'] or not _is_callback(app, values):
        message = _('Use the authorization code flow and the exact registered callback URL.')
        return _oauth_error('invalid_request', message)
    if not _is_resource(request.values):
        return _oauth_error('invalid_target', _('The resource must be this Indico MCP server.'))
    if len(values.getlist('code_verifier')) != 1 or not values['code_verifier']:
        return _oauth_error('invalid_request', _('The PKCE verifier is required.'))

    # Indico loads and consumes the authorization code separately. Serialize exchanges
    # per client so concurrent requests cannot redeem the same code twice.
    if OAuthApplication.query.filter_by(id=app.id).with_for_update(of=OAuthApplication).one_or_none() is None:
        return _oauth_error('invalid_client', _('The registered client is unavailable.'))
    return None
