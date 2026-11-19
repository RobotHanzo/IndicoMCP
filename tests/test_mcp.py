from typing import Annotated

import pytest
from pydantic import Field

from indico_mcp.protocol import META_PREFIX, MODERN_VERSION, dispatch
from indico_mcp.registry import Registry, ToolError


@pytest.fixture
def registry():
    tools = Registry()

    @tools.tool(idempotent=True)
    def indico_read(user, event_id: Annotated[int, Field(ge=1)]):
        """Read an event."""
        return {'actor': user, 'event_id': event_id}

    @tools.tool(read_only=False, destructive=True, open_world=True)
    def indico_write(user, confirm: bool):
        """Update an event."""
        return {'confirmed': confirm}

    return tools


def test_registry_lists_tools_by_scope(registry):
    assert [tool['name'] for tool in registry.list(can_write=False)] == ['indico_read']
    read, write = registry.list(can_write=True)
    assert read['inputSchema']['required'] == ['event_id']
    assert read['inputSchema']['properties']['event_id']['minimum'] == 1
    assert read['inputSchema']['additionalProperties'] is False
    assert write['annotations'] == {
        'readOnlyHint': False, 'destructiveHint': True, 'idempotentHint': False, 'openWorldHint': True,
    }
    assert registry.get('indico_write', can_write=False) is None


@pytest.mark.parametrize('arguments', [
    {'event_id': '9'}, {'event_id': 0}, {'event_id': True}, {'unexpected': 'secret'},
])
def test_invalid_arguments_are_strict_and_do_not_echo_inputs(registry, arguments):
    tool = registry.get('indico_read', can_write=False)
    with pytest.raises(ToolError) as failure:
        tool.call(12, arguments)
    assert failure.value.details
    assert 'secret' not in str(failure.value.details)
    assert 'unexpected' not in str(failure.value.details)


def test_registry_uses_callers_identity(registry):
    tool = registry.get('indico_read', can_write=False)
    assert tool.call(12, {'event_id': 9}) == {'actor': 12, 'event_id': 9}
    with pytest.raises(ToolError):
        registry.get('indico_write', can_write=True).call(12, {'confirm': 1})


def _request(method, params=None):
    return {'jsonrpc': '2.0', 'id': 13, 'method': method, 'params': params or {}}


def _modern(method, params=None):
    headers = {'MCP-Protocol-Version': MODERN_VERSION, 'Mcp-Method': method}
    meta = {
        f'{META_PREFIX}protocolVersion': MODERN_VERSION,
        f'{META_PREFIX}clientInfo': {'name': 'test', 'version': '1.0'},
        f'{META_PREFIX}clientCapabilities': {},
    }
    return _request(method, {**(params or {}), '_meta': meta}), headers


def _unexpected(*args):
    pytest.fail('Unexpected tool dispatch')


def test_legacy_handshake_and_tool_discovery(registry):
    request = _request('initialize', {
        'protocolVersion': '2025-06-18', 'clientInfo': {'name': 'test', 'version': '1.0'}, 'capabilities': {},
    })
    response, status = dispatch(request, {}, _unexpected, _unexpected)
    assert status == 200
    assert response['id'] == 13
    assert response['result']['protocolVersion'] == '2025-06-18'
    response, status = dispatch(_request('tools/list'), {'MCP-Protocol-Version': '2025-06-18'},
                                lambda: registry.list(can_write=False), _unexpected)
    assert status == 200
    assert [tool['name'] for tool in response['result']['tools']] == ['indico_read']


def test_modern_discovery_and_call_validate_mirrored_headers():
    request, headers = _modern('server/discover')
    response, status = dispatch(request, headers, _unexpected, _unexpected)
    assert status == 200
    assert response['result']['resultType'] == 'complete'
    assert MODERN_VERSION in response['result']['supportedVersions']

    request, headers = _modern('tools/call', {'name': 'indico_read', 'arguments': {'event_id': 42}})
    response, status = dispatch(request, {**headers, 'Mcp-Name': 'indico_write'}, _unexpected, _unexpected)
    assert status == 400
    assert response['error']['code'] == -32020
    response, status = dispatch(request, {**headers, 'Mcp-Name': '=?base64?aW5kaWNvX3JlYWQ=?='}, _unexpected,
                                lambda name, args: {'structuredContent': args})
    assert status == 200
    assert response['result']['structuredContent'] == {'event_id': 42}


def test_modern_unknown_version_advertises_supported_versions():
    request, headers = _modern('server/discover')
    request['params']['_meta'][f'{META_PREFIX}protocolVersion'] = '2030-01-01'
    headers['MCP-Protocol-Version'] = '2030-01-01'
    response, status = dispatch(request, headers, _unexpected, _unexpected)
    assert status == 400
    assert response['error']['code'] == -32022
    assert MODERN_VERSION in response['error']['data']['supported']


def test_notifications_and_batches_cannot_execute_tools():
    request = _request('tools/call', {'name': 'indico_write', 'arguments': {'confirm': True}})
    del request['id']
    assert dispatch(request, {}, _unexpected, _unexpected) == (None, 202)
    response, status = dispatch([request], {}, _unexpected, _unexpected)
    assert status == 400
    assert response['error']['code'] == -32600
