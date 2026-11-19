import os

import pytest

POSTGRES_URI = os.environ.get('INDICO_MCP_TEST_POSTGRES')
TEST_ALL_PLUGINS = os.environ.get('INDICO_MCP_TEST_ALL_PLUGINS') == '1'
TEST_GROUP_REGISTRATION = TEST_ALL_PLUGINS or os.environ.get('INDICO_MCP_TEST_GROUP_REGISTRATION') == '1'
pytestmark = pytest.mark.skipif(not POSTGRES_URI, reason='Set INDICO_MCP_TEST_POSTGRES to a test PostgreSQL server')


def _rpc(client, token_value, method, params=None):
    headers = {'MCP-Protocol-Version': '2025-11-25', 'Accept': 'application/json'}
    if token_value is not None:
        headers['Authorization'] = f'Bearer {token_value}'
    return client.post('/mcp', headers=headers, json={'jsonrpc': '2.0', 'id': 1, 'method': method,
                                                      'params': params or {}})


def _call(client, token_value, tool, *, error=False, **arguments):
    response = _rpc(client, token_value, 'tools/call', {'name': tool, 'arguments': arguments})
    payload = response.get_json()
    assert response.status_code == 200 and 'result' in payload, payload
    result = payload['result']
    assert result.get('isError', False) is error, result
    return result['structuredContent']
