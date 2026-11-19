from indico.core.db import db
from indico.core.db.sqlalchemy import UTCDateTime
from indico.util.date_time import now_utc


class OAuthClientRegistration(db.Model):
    """Additional restrictions for OAuth clients registered through MCP."""

    __tablename__ = 'oauth_client_registrations'
    __table_args__ = {'schema': 'plugin_mcp'}

    application_id = db.Column(db.Integer, db.ForeignKey('oauth.applications.id', ondelete='CASCADE'), primary_key=True)
    auth_method = db.Column(db.String, nullable=False)
    created_dt = db.Column(UTCDateTime, nullable=False, default=now_utc)

    application = db.relationship('OAuthApplication')
