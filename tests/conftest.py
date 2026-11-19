import os
from datetime import UTC, datetime, timedelta
from importlib.metadata import entry_points
from uuid import uuid4

import pytest
from integration_support import POSTGRES_URI, TEST_ALL_PLUGINS, TEST_GROUP_REGISTRATION, _call


@pytest.fixture(scope='session')
def database_uri():
    from flask import Flask
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url

    admin_url = make_url(POSTGRES_URI)
    assert admin_url.get_backend_name() == 'postgresql', 'The integration tests require PostgreSQL'
    admin_engine = create_engine(admin_url, isolation_level='AUTOCOMMIT')
    maintenance_app = Flask('indico-mcp-test-maintenance')
    database = f'indico_mcp_test_{uuid4().hex}'
    database_url = admin_url.set(database=database)
    with admin_engine.connect() as conn:
        conn.exec_driver_sql(f'CREATE DATABASE {database} TEMPLATE template0 ENCODING \'UTF8\'')

    try:
        engine = create_engine(database_url, isolation_level='AUTOCOMMIT')
        try:
            with engine.connect() as conn:
                conn.exec_driver_sql('CREATE EXTENSION unaccent')
                conn.exec_driver_sql('CREATE EXTENSION pg_trgm')
        finally:
            engine.dispose()
        yield database_url.render_as_string(hide_password=False)
    finally:
        # Only the randomly named database created by this fixture is removed.
        with maintenance_app.app_context(), admin_engine.connect() as conn:
            conn.exec_driver_sql(f'DROP DATABASE {database}')
        admin_engine.dispose()


@pytest.fixture(scope='session')
def indico_app(database_uri, tmp_path_factory):
    from unittest.mock import patch

    from flask_caching.backends.simplecache import SimpleCache
    from indico.core.cache import CachedNone
    from indico.core.db import db
    from indico.core.db.sqlalchemy.util.management import create_all_tables
    from indico.web.flask import app as app_factory

    class TestCache(SimpleCache):
        def get(self, key, default=None):
            return CachedNone.unwrap(super().get(key), default)

        def get_many(self, *keys, default=None):
            return [self.get(key, default=default) for key in keys]

        def get_dict(self, *keys, default=None):
            return {key: self.get(key, default=default) for key in keys}

        def set(self, key, value, timeout=None):
            return super().set(key, CachedNone.wrap(value), timeout)

    def configure_cache(app, config):
        app.config.update(CACHE_TYPE='indico.web.flask.app._mcp_test_cache', CACHE_DEFAULT_TIMEOUT=0)

    real_configure_db = app_factory.configure_db

    def configure_db(app):
        app.config['SQLALCHEMY_DATABASE_URI'] = database_uri
        real_configure_db(app)

    plugins = {'mcp'}
    if TEST_GROUP_REGISTRATION:
        plugins.add('group_registration')
    if TEST_ALL_PLUGINS:
        plugins.update({'stsa', 'sentry'})
    missing = plugins - {entry.name for entry in entry_points(group='indico.plugins')}
    assert not missing, f'Install these Indico plugins in the test environment: {sorted(missing)}'
    directory = str(tmp_path_factory.mktemp('indico-data'))
    config = {
        'BASE_URL': 'http://localhost',
        'SECRET_KEY': os.urandom(32),
        'SQLALCHEMY_DATABASE_URI': database_uri,
        'TEMP_DIR': directory,
        'CACHE_DIR': directory,
        'STORAGE_BACKENDS': {'default': 'mem:'},
        'SMTP_USE_CELERY': False,
        'EMAIL_BACKEND': 'indico.vendor.django_mail.backends.locmem.EmailBackend',
        'NO_REPLY_EMAIL': 'noreply@example.test',
        'ENABLE_ROOMBOOKING': True,
        'PLUGINS': plugins,
    }
    with (patch.object(app_factory, '_mcp_test_cache', TestCache, create=True),
          patch.object(app_factory, 'configure_cache', configure_cache),
          patch.object(app_factory, 'configure_db', configure_db)):
        app = app_factory.make_app(testing=True, config_override=config)

    try:
        with app.app_context():
            create_all_tables(db)
        yield app
    finally:
        with app.app_context():
            db.session.remove()
            db.engine.dispose()


@pytest.fixture(scope='session')
def token(indico_app):
    from indico.core.db import db
    from indico.core.oauth.models.personal_tokens import PersonalToken
    from indico.modules.users import User

    with indico_app.app_context():
        users = {}
        for name, admin in (('manager', True), ('participant', False), ('other', False)):
            user = User(first_name=name.title(), last_name='Test', is_admin=admin, email=f'{name}@example.test')
            db.session.add(user)
            users[name] = user
        scopes_by_role = {
            'manager': ('manager', ['read:mcp', 'write:mcp']),
            'read_only': ('manager', ['read:mcp']),
            'unscoped': ('manager', ['read:user']),
            'participant': ('participant', ['read:mcp', 'write:mcp']),
            'other': ('other', ['read:mcp', 'write:mcp']),
        }
        tokens = {}
        for role, (user_key, scopes) in scopes_by_role.items():
            token_row = PersonalToken(name=role, user=users[user_key], scopes=scopes)
            tokens[role] = token_row.generate_token()
            db.session.add(token_row)
        db.session.commit()
        return tokens


@pytest.fixture(scope='session')
def public_event(indico_app, token):
    client = indico_app.test_client()
    category = _call(client, token['manager'], 'indico_create_category', parent_id=0, title='MCP integration category')
    day = datetime.now(UTC) + timedelta(days=30)
    event = _call(client, token['manager'], 'indico_create_event', category_id=category['id'],
                  title='MCP integration event', event_type='conference', start_dt=day.isoformat(),
                  end_dt=(day + timedelta(hours=2)).isoformat())
    return category, event
