import base64
import binascii

from indico_mcp import __version__

MODERN_VERSION = '2026-07-28'
LEGACY_VERSIONS = ('2025-11-25', '2025-06-18', '2025-03-26')
VERSIONS = (MODERN_VERSION, *LEGACY_VERSIONS)
SERVER_INFO = {'name': 'indico-mcp', 'version': __version__}
INSTRUCTIONS = 'Manage Indico using the authenticated account and its Indico permissions.'
META_PREFIX = 'io.modelcontextprotocol/'
META_VERSION = f'{META_PREFIX}protocolVersion'
CAPABILITIES = {'tools': {}}
NAMED_METHODS = {'tools/call': 'name', 'resources/read': 'uri', 'prompts/get': 'name'}


def rpc_error(code, message, request_id=None, *, data=None):
    error = {'code': code, 'message': message}
    if data is not None:
        error['data'] = data
    return {'jsonrpc': '2.0', 'id': request_id, 'error': error}


def _rpc_result(request_id, result):
    return {'jsonrpc': '2.0', 'id': request_id, 'result': result}


def _decode_name(value):
    if value is None or not (value.startswith('=?base64?') and value.endswith('?=')):
        return value
    try:
        return base64.b64decode(value[9:-2], validate=True).decode('utf-8')
    except (ValueError, binascii.Error):
        return None


def _modern_header_error(headers, method, params, version):
    if version is None or headers.get('MCP-Protocol-Version') != version or headers.get('Mcp-Method') != method:
        return True
    if argument := NAMED_METHODS.get(method):
        value = params.get(argument)
        return not isinstance(value, str) or _decode_name(headers.get('Mcp-Name')) != value
    return headers.get('Mcp-Name') is not None


def dispatch(message, headers, list_tools, call_tool):
    """Dispatch one stateless JSON-RPC message and return (response, HTTP status)."""
    if not isinstance(message, dict):
        return rpc_error(-32600, 'Invalid Request'), 400
    request_id = message.get('id')
    is_request = 'id' in message
    method = message.get('method')
    if (message.get('jsonrpc') != '2.0' or not isinstance(method, str) or not method
            or (is_request and type(request_id) not in (str, int))):
        return rpc_error(-32600, 'Invalid Request'), 400
    if not is_request:
        # Never execute requests, especially write tools, sent without a reply ID.
        return None, 202

    params = message.get('params', {})
    if not isinstance(params, dict) or not isinstance(params.get('_meta', {}), dict):
        return rpc_error(-32602, 'Invalid params', request_id), 400
    meta = params.get('_meta', {})
    header_version = headers.get('MCP-Protocol-Version')
    modern = META_VERSION in meta or method == 'server/discover' or header_version == MODERN_VERSION
    if modern:
        version = meta.get(META_VERSION)
        if _modern_header_error(headers, method, params, version):
            return rpc_error(-32020, 'MCP headers do not match the request body', request_id), 400
        if version != MODERN_VERSION:
            return rpc_error(-32022, 'Unsupported protocol version', request_id,
                             data={'requested': version, 'supported': list(VERSIONS)}), 400
        client = meta.get(f'{META_PREFIX}clientInfo')
        if (not isinstance(client, dict) or not isinstance(client.get('name'), str)
                or not isinstance(client.get('version'), str)
                or not isinstance(meta.get(f'{META_PREFIX}clientCapabilities'), dict)):
            return rpc_error(-32602, 'Missing or invalid MCP client metadata', request_id), 400
    elif header_version is not None and header_version not in LEGACY_VERSIONS:
        return rpc_error(-32600, 'Unsupported MCP-Protocol-Version', request_id), 400

    if method == 'server/discover':
        result = {
            'supportedVersions': list(VERSIONS),
            'capabilities': CAPABILITIES,
            '_meta': {f'{META_PREFIX}serverInfo': SERVER_INFO},
            'instructions': INSTRUCTIONS,
        }
    elif method == 'initialize' and not modern:
        requested = params.get('protocolVersion')
        client = params.get('clientInfo')
        if (not isinstance(requested, str) or not isinstance(params.get('capabilities'), dict)
                or not isinstance(client, dict) or not isinstance(client.get('name'), str)
                or not isinstance(client.get('version'), str)):
            return rpc_error(-32602, 'Invalid initialize parameters', request_id), 400
        result = {
            'protocolVersion': requested if requested in LEGACY_VERSIONS else LEGACY_VERSIONS[0],
            'capabilities': CAPABILITIES,
            'serverInfo': SERVER_INFO,
            'instructions': INSTRUCTIONS,
        }
    elif method == 'ping':
        result = {}
    elif method == 'tools/list':
        if params.get('cursor') is not None:
            return rpc_error(-32602, 'Invalid cursor', request_id), 400 if modern else 200
        result = {'tools': list_tools()}
    elif method == 'tools/call':
        name, arguments = params.get('name'), params.get('arguments', {})
        if not isinstance(name, str) or not isinstance(arguments, dict):
            return rpc_error(-32602, 'Invalid tool call', request_id), 400 if modern else 200
        result = call_tool(name, arguments)
        if result is None:
            return rpc_error(-32602, 'Tool unavailable', request_id), 400 if modern else 200
        if not modern and header_version not in ('2025-11-25', '2025-06-18'):
            result = {key: value for key, value in result.items() if key != 'structuredContent'}
    else:
        return rpc_error(-32601, 'Method not found', request_id), 404 if modern else 200

    if modern:
        result = {'resultType': 'complete', **result}
    return _rpc_result(request_id, result), 200
