"""Track automatically registered MCP OAuth clients.

Revision ID: 41666a9de3b2
Revises:
Create Date: 2026-11-19 00:00:00
"""

import sqlalchemy as sa
from alembic import op
from indico.core.db.sqlalchemy import UTCDateTime

revision = '41666a9de3b2'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.execute('CREATE SCHEMA plugin_mcp')
    op.create_table(
        'oauth_client_registrations',
        sa.Column('application_id', sa.Integer(), sa.ForeignKey('oauth.applications.id', ondelete='CASCADE'),
                  primary_key=True),
        sa.Column('auth_method', sa.String(), nullable=False),
        sa.Column('created_dt', UTCDateTime, nullable=False),
        schema='plugin_mcp',
    )


def downgrade():
    op.execute(
        'DELETE FROM oauth.applications WHERE id IN (SELECT application_id FROM plugin_mcp.oauth_client_registrations)'
    )
    op.drop_table('oauth_client_registrations', schema='plugin_mcp')
    op.execute('DROP SCHEMA plugin_mcp')
