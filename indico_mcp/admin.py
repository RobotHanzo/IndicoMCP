from uuid import uuid4

from flask import request, session
from indico.core.db import db
from indico.core.oauth.models.personal_tokens import PersonalToken
from indico.core.plugins import render_plugin_template, url_for_plugin
from indico.modules.admin import RHAdminBase
from indico.modules.logs.models.entries import LogKind, UserLogRealm
from indico.util.i18n import _
from indico.web.flask.util import url_for
from indico.web.util import get_request_user, jsonify_data
from werkzeug.exceptions import BadRequest, Forbidden

from indico_mcp.oauth import automatic_registration_available, mcp_url
from indico_mcp.plugin import ACCESS_SCOPES


def render_setup():
    return render_plugin_template(
        'setup.html',
        server_url=mcp_url(),
        automatic_registration=automatic_registration_available(),
        create_token_url=url_for_plugin('mcp.create_token'),
        manage_tokens_url=url_for('oauth.user_tokens'),
    )


class RHCreateMCPToken(RHAdminBase):
    def _check_access(self):
        super()._check_access()
        if get_request_user()[1] != 'session':
            raise Forbidden(_('Sign in to Indico in your browser to generate an MCP token.'))

    def _process_POST(self):
        access = request.form.get('access')
        if access not in ACCESS_SCOPES:
            raise BadRequest(_('Choose read-only or read/write access.'))

        user = session.user
        token = PersonalToken(user=user, name=f'Indico MCP ({access}) {uuid4().hex[:12]}', scopes=ACCESS_SCOPES[access])
        access_token = token.generate_token()
        db.session.add(token)
        db.session.flush()
        user.log(UserLogRealm.user, LogKind.positive, 'API Tokens', 'Token created via Indico MCP', user,
                 data={'ID': token.id, 'Name': token.name, 'Scopes': ' '.join(sorted(token.scopes)),
                       'IP': request.remote_addr}, meta={'token_id': token.id})

        return jsonify_data(flash=False, html=render_plugin_template(
            'token_created.html', token_name=token.name, access_token=access_token,
            manage_tokens_url=url_for('oauth.user_tokens'),
        ))
