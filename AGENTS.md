# Repository Guidelines

## Project Structure

- `indico_mcp/` contains the plugin. `plugin.py` wires the Indico entry point and tool registry. `endpoint.py`, `protocol.py`, and `registry.py` handle `/mcp`, protocol dispatch, and typed tools.
- `core_*.py`, `registrations.py`, and `room_booking.py` implement Indico workflows. `event_extensions.py` and `stsa_extensions.py` integrate optional STSA, Group Registration, and Sentry plugins.
- `indico_mcp/templates/` holds Jinja templates and `indico_mcp/migrations/` holds Alembic migrations. Tests live in `tests/`. GitHub Actions workflows live in `.github/workflows/`.

## Build, Test, and Development Commands

Use Python 3.12.2 or later within the 3.12 series and Indico 3.3.14.

- `python -m pip install -e '.[dev]'` installs the plugin in editable mode with pytest and Ruff.
- `ruff check .` checks style and imports. `python -m pytest` runs the test suite.
- `python -m pip install build twine` installs the packaging tools. Run `python -m build` to generate a wheel and source archive, then `python -m twine check --strict dist/*` to validate their metadata.

For a local Indico instance, add `mcp` to `PLUGINS` in `indico.conf` and run `indico db --plugin mcp upgrade`. Start Indico with `indico run -h 127.0.0.1 -p 8000` using a matching `BASE_URL`.

## Coding Style and Naming

Use four spaces, `snake_case` for functions and modules, `PascalCase` for classes, and a 120-character line limit. Name tools `indico_<action>_<object>`. Accept the caller as the first argument, type the remaining arguments, and give the tool an actionable docstring. Mark mutations with `read_only=False` and enforce native Indico permissions.

## Testing

Name test files `test_*.py` and test functions `test_*`. Reuse fixtures in `tests/conftest.py`. Set `INDICO_MCP_TEST_POSTGRES` to a disposable PostgreSQL maintenance DSN with permission to create and drop test databases and install `unaccent` and `pg_trgm`. Set `INDICO_MCP_TEST_ALL_PLUGINS=1` with the optional plugins installed to exercise their integrations. CI tests the installed wheel in both configurations. For write tools, cover permission denials and verify persisted changes.

## Commits and Pull Requests

Use imperative commit subjects, such as `Enable registration form field edits and moves through MCP`. In PRs, summarize behavior changes, report tests run, link related issues, and flag migrations or configuration changes. Include screenshots for visible UI changes. Release tags use `v<version>` and match `pyproject.toml`.
