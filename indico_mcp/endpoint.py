import json
from urllib.parse import urlsplit

import sentry_sdk
from authlib.oauth2 import OAuth2Error
from flask import current_app, jsonify, request
from indico.core.config import config
from indico.core.errors import UserValueError
from indico.core.oauth import require_oauth
from indico.core.plugins import IndicoPluginBlueprint, current_plugin
from indico.web.rh import RH
from indico.web.util import get_request_user
from werkzeug.exceptions import HTTPException, Unauthorized

from indico_mcp.admin import RHCreateMCPToken
from indico_mcp.oauth import RHProtectedResourceMetadata, RHRegisterOAuthClient, add_discovery_challenge
from indico_mcp.plugin import EXTENSION_NAME, READ_SCOPE, WRITE_SCOPE
from indico_mcp.protocol import dispatch, rpc_error
from indico_mcp.registry import ToolError

blueprint = IndicoPluginBlueprint('mcp', __name__)
blueprint.add_url_rule('!/admin/plugins/mcp/token', 'create_token', RHCreateMCPToken, methods=('POST',))
blueprint.add_url_rule('!/.well-known/oauth-protected-resource/mcp', 'protected_resource_metadata',
                       RHProtectedResourceMetadata)
blueprint.add_url_rule('!/.well-known/oauth-protected-resource', 'root_resource_metadata', RHProtectedResourceMetadata)
blueprint.add_url_rule('!/mcp/oauth/register', 'register_client', RHRegisterOAuthClient, methods=('POST',))
MAX_REQUEST_BYTES = 1024 * 1024


def _bearer_token():
    try:
        # Indico's validator handles token revocation, expiry and last-use auditing.
        return require_oauth.acquire_token([READ_SCOPE])
    except OAuth2Error as exc:
        require_oauth.raise_error_response(exc)


def _reject_nonfinite(value):
    raise ValueError('Non-finite JSON number')


def _scrub_mcp_event(event, hint):
    request_data = event.get('request', {})
    request_data.pop('data', None)
    request_data.pop('cookies', None)
    request_data.pop('query_string', None)
    headers = request_data.get('headers') or {}
    for key in list(headers):
        if key.lower() in {'authorization', 'cookie'}:
            headers.pop(key)
    for exception in event.get('exception', {}).get('values', []):
        for frame in exception.get('stacktrace', {}).get('frames', []):
            frame.pop('vars', None)
    return event


def _format_result(value):
    if not isinstance(value, dict):
        raise TypeError('MCP tools must return an object')
    text = current_app.json.dumps(value, ensure_ascii=False, allow_nan=False)
    return {'content': [{'type': 'text', 'text': text}], 'structuredContent': value}


def _format_tool_error(message, *, details=None):
    value = {'error': message}
    if details is not None:
        value['details'] = details
    return {**_format_result(value), 'isError': True}


class RHMCP(RH):
    _OAUTH_SCOPE = READ_SCOPE
    request_id = None

    def _check_csrf(self):
        # The MCP endpoint authenticates each request with a bearer token. Reuse
        # Indico's request identity for permission checks, events and notifications.
        token = _bearer_token()
        self.user, source = get_request_user()
        if self.user is None or source != 'oauth':
            raise Unauthorized('An Indico user bearer token is required')
        self.can_write = WRITE_SCOPE in token.get_scope().split()

    def _call_tool(self, name, arguments):
        tool = self.registry.get(name, can_write=self.can_write)
        if tool is None:
            return None
        current_plugin.logger.info('MCP tool %s invoked by user %s', name, self.user.id)
        try:
            return _format_result(tool.call(self.user, arguments))
        except ToolError as exc:
            self.commit = False
            return _format_tool_error(str(exc), details=exc.details)
        except UserValueError as exc:
            self.commit = False
            return _format_tool_error(str(exc))
        except HTTPException as exc:
            if exc.code is None or not 400 <= exc.code < 500:
                raise
            self.commit = False
            description = exc.name if exc.code in (401, 403, 404) else str(exc.description)
            return _format_tool_error(description)

    def _process(self):
        request.max_content_length = MAX_REQUEST_BYTES
        try:
            message = json.loads(request.get_data().decode('utf-8'), parse_constant=_reject_nonfinite)
        except (ValueError, RecursionError):
            self.commit = False
            return jsonify(rpc_error(-32700, 'Parse error')), 400

        if isinstance(message, dict) and type(message.get('id')) in (str, int):
            self.request_id = message['id']
        self.registry = current_app.extensions[EXTENSION_NAME]
        result, status = dispatch(message, request.headers, lambda: self.registry.list(can_write=self.can_write),
                                  self._call_tool)
        if status != 200:
            self.commit = False
        if result is None:
            return '', status
        return jsonify(result), status


@blueprint.after_request
def _protect_response(response):
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    if request.endpoint == 'plugin_mcp.mcp_endpoint' and response.status_code == 401:
        add_discovery_challenge(response)
    return response


@blueprint.before_request
def _protect_request_trace():
    sentry_sdk.get_isolation_scope().add_event_processor(_scrub_mcp_event)


@blueprint.route('!/mcp', methods=('GET', 'POST', 'DELETE'), provide_automatic_options=False)
def mcp_endpoint():
    base_url = urlsplit(config.BASE_URL)
    origin = request.headers.get('Origin')
    if origin is not None and origin != f'{base_url.scheme}://{base_url.netloc}':
        return jsonify(rpc_error(-32600, 'Invalid Origin')), 403
    if request.method != 'POST':
        return '', 405, {'Allow': 'POST'}
    if request.query_string:
        return jsonify(rpc_error(-32600, 'The MCP endpoint does not accept URL query parameters')), 400
    if request.mimetype != 'application/json':
        return jsonify(rpc_error(-32600, 'Content-Type must be application/json')), 415
    if request.headers.get('Accept') and not request.accept_mimetypes['application/json']:
        return jsonify(rpc_error(-32600, 'Accept must allow application/json')), 406
    handler = RHMCP()
    try:
        return handler.process()
    except HTTPException:
        # Indico handles bearer challenges and their OAuth headers.
        raise
    except Exception:
        if current_app.testing or current_app.debug:
            raise
        current_plugin.logger.exception('Unhandled MCP request error')
        sentry_sdk.capture_exception()
        message = 'Indico could not complete the request. Check its state before retrying.'
        return jsonify(rpc_error(-32603, message, handler.request_id)), 500
