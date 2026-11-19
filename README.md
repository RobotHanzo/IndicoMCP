# Indico MCP

Indico MCP gives AI agents typed tools for Indico event management and participant workflows. It runs inside Indico and acts as the account that authorized each request. Indico permissions, event protection, registration visibility, feature flags, and supported plugin rules apply to individual calls.

## Install

Install into an existing **Indico 3.3.14** environment running **Python 3.12**.

```sh
python -m pip install .
```

Add `mcp` to `PLUGINS` in `indico.conf` and keep the existing entries. A configuration with all supported optional plugins enabled looks like this.

```python
PLUGINS = {'mcp', 'stsa', 'group_registration', 'sentry'}
```

Run the MCP database migration on installation or when upgrading from a release without automatic OAuth registration, then restart the Indico web workers.

```sh
indico db --plugin mcp upgrade
```

The optional plugins must be installed, migrated, and configured through their own setup procedures. The adapters target the STSA directory's STSA 0.6.2, Group Registration 0.7.0, and Sentry 1.1.1. A core-only deployment uses `PLUGINS = {'mcp'}`. [Indico plugin installation documentation](https://docs.getindico.io/en/stable/installation/plugins/)

## Connect an agent

### Automatic OAuth with Claude

Add the public HTTPS server URL, such as `https://indico.example.org/mcp`, as a custom connector in Claude. Under **OAuth client**, choose **Register automatically**, then sign in to Indico and authorize the requested scopes. Claude’s callback URL is approved by default. [Claude custom connector setup](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp?rd=1)

Under **Administration → Plugins → Indico MCP**, set **Default access for automatic OAuth** to **Read-only** (`read:mcp`) or **Read/write** (`read:mcp write:mcp`). Clients that supply their own MCP scopes can request them explicitly during registration and authorization. When a connector requests write access, the Indico consent screen offers both **Authorize read/write** and **Authorize read-only**. Re-add an existing read-only connector to register it with the new default, then authorize it in Indico. Revoke old OAuth connections under **Applications** in your Indico profile.

Administrators can add exact HTTPS callback URLs for other clients on the same plugin settings page. Clearing the list disables automatic registration. Registered clients use authorization code with PKCE S256.

For an Indico installation mounted below the site root, expose the well-known OAuth URLs through your reverse proxy. With `BASE_URL=https://indico.example.org/indico`, forward these requests internally and preserve the public origin. [MCP authorization discovery](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization#authorization-server-discovery)

| Public request path | Indico path |
| --- | --- |
| `/.well-known/oauth-authorization-server/indico` | `/indico/.well-known/oauth-authorization-server` |
| `/.well-known/oauth-protected-resource/indico/mcp` | `/indico/.well-known/oauth-protected-resource/mcp` |

### Bearer tokens

Administrators can open **Administration → Plugins → Indico MCP** to copy the server URL and generate a read-only or read/write token for their signed-in account. Copy the token from the dialog when it appears. You can rename, reset, or revoke it later in **API Tokens**.

1. In the Indico account that will perform the work, open **API Tokens** and create a dedicated token. Select **Read Indico data through MCP** (`read:mcp`). Add **Perform Indico actions through MCP** (`write:mcp`) for agents that need writes. If token management is restricted, an Indico administrator can assign the scopes.
2. Configure the client with the **Streamable HTTP** URL `https://indico.example.org/mcp`. Include the installation prefix if Indico lives under a subpath. Supply the token through the client's secret store as `Authorization: Bearer <token>`.
3. Use `tools/list` to discover the available tools and `tools/call` to invoke them. Begin with the read tools to find record IDs and validate the intended changes.

Indico personal API tokens and Indico OAuth user tokens with the MCP scopes are accepted. Manually registered OAuth applications can also use Indico’s authorization and token endpoints. The endpoint supports request-scoped JSON responses for MCP `2026-07-28`, `2025-11-25`, `2025-06-18`, and `2025-03-26`. Browser requests that send an `Origin` must match the Indico installation origin. [Indico API token documentation](https://docs.getindico.io/en/stable/http-api/access/#api-token-authentication) · [MCP Streamable HTTP specification](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http)

Example with a legacy-protocol client. Set `INDICO_MCP_TOKEN` in your shell before running it.

```sh
curl --fail-with-body https://indico.example.org/mcp \
  -H "Authorization: Bearer $INDICO_MCP_TOKEN" \
  -H 'Accept: application/json, text/event-stream' \
  -H 'Content-Type: application/json' \
  -H 'MCP-Protocol-Version: 2025-11-25' \
  --data '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

## Workflows

| Area | Tools |
| --- | --- |
| Categories and events | Browse and manage categories and events, location, scheduling, moves, locks, and event features |
| Programme and materials | Manage contributions and subcontributions, sessions, timetable entries, material folders, and links |
| Registration | Manage forms, sections, fields, and schedules. Discover field options and defaults, submit or edit a registration, respond to invitations, retrieve ticket links, moderate and withdraw registrations, check in participants, and record offline payments |
| Abstracts and surveys | Manage calls for abstracts, submit and update abstracts, manage surveys, and save and submit survey answers |
| Account | Read and update your profile, preferences, and favorites, and list your linked events |
| Room booking | Search rooms and availability, reserve or request rooms, view and cancel your bookings, and moderate requests when room booking is enabled |
| STSA (`stsa`) | Read the caller's registered events, manage STSA registration settings and profile fields, recalculate discounts, preview and send payment reminders, manage non-secret site settings, install packaged ticket templates, and inspect STSA API documentation |
| Group Registration (`group_registration`) | Discover group plans and disclaimers, create or join through a registration form, view and manage groups, validate membership codes, inspect balances, record offline payments, and preview and send payment reminders |
| Sentry (`sentry`) | Administrator status, selected settings, diagnostics and reload for the serving worker |

For self-registration, obtain the field names and options with `indico_get_registration_form` before submitting. Group Registration's plan selector and STSA's membership fields are provided by the installed plugins. These fields go through Indico's native registration validation and signals. For Wallet passes, open the link from `indico_get_registration_ticket_links` in a signed-in Indico browser. The `apple-wallet-badges` sibling repository supplies artwork and has no separate runtime. Financials, MembershipAPI, MembershipCard, and PortalWeaveSSO are separate applications. STSA's in-Indico workflows still use its configured membership integration.

To reorganize a registration form, use `indico_get_registration_form` with `management: true` to find field and section IDs. Use `indico_update_registration_field` to change a title or description, including for built-in fields. Use `indico_move_registration_field` with `after_field_id` to place a field after another item on the same form. To put a field first in a section, pass `section_id` and omit `after_field_id`. After removing or moving all items from a custom section, use `indico_delete_registration_section` with `confirm: true`.

### Browser and operator workflows

Open payment-provider checkout, file uploads, generated downloads, and visual editors in Indico. Use `indico_get_event_management_links` and `indico_get_site_admin_links` to find browser pages available to your account. Use native admin forms for destinations, keys, access policy, and Sentry privacy or SDK policy. Host maintenance and operator crash testing require deployment access. Abstract reviewing and judgement, paper reviewing, editing workflows, and other module actions outside the table still use the Indico UI.

## Development

```sh
python -m pip install -e '.[dev]'
python -m pytest
ruff check .
```

Set `INDICO_MCP_TEST_POSTGRES` to a dedicated PostgreSQL test server's maintenance DSN to run the integration tests. The test account needs permission to create and drop its randomly named test database and install `unaccent` and `pg_trgm` in that database. Set `INDICO_MCP_TEST_GROUP_REGISTRATION=1` to exercise Group Registration or `INDICO_MCP_TEST_ALL_PLUGINS=1` to test all three installed optional plugins together.

## Releases

GitHub Actions builds the wheel and source distribution, checks their PyPI metadata, and tests the wheel with PostgreSQL in core-only and optional-plugin configurations. Publishing a GitHub release runs those same checks on the tagged commit before uploading the artifacts to PyPI.

The GitHub `pypi` environment accepts `v*` release tags. Add a pending GitHub Actions Trusted Publisher for the first release using the values below. For an existing PyPI project, use its Publishing settings. [GitHub environment settings](https://github.com/RobotHanzo/IndicoMCP/settings/environments) · [PyPI account publishing settings](https://pypi.org/manage/account/publishing/) · [PyPI Trusted Publishing documentation](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/)

| Field | Value |
| --- | --- |
| PyPI project | `indico-plugin-mcp` |
| GitHub owner | `RobotHanzo` |
| GitHub repository | `IndicoMCP` |
| Workflow | `publish.yml` |
| Environment | `pypi` |

Push the workflows and the package version in `pyproject.toml` to `main`. Once CI passes, publish a GitHub release from that commit using the `v<version>` tag. The first release uses `v0.1.0`. Keep the GitHub release as a draft until you are ready to publish to PyPI.
