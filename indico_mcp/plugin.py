from operator import itemgetter

from indico.core import signals
from indico.core.config import config
from indico.core.oauth.oauth2 import auth_server
from indico.core.oauth.scopes import SCOPES
from indico.core.plugins import IndicoPlugin, plugin_engine
from indico.modules.oauth.forms import ApplicationForm, PersonalTokenForm
from indico.util.i18n import _

from indico_mcp.forms import CLAUDE_CALLBACK, OAuthSettingsForm
from indico_mcp.registry import Registry

READ_SCOPE = 'read:mcp'
WRITE_SCOPE = 'write:mcp'
EXTENSION_NAME = 'indico_mcp'


class MCPPlugin(IndicoPlugin):
    """Indico MCP

    Expose Model Context Protocol tools for Indico and supported active plugins.
    """

    configurable = True
    settings_form = OAuthSettingsForm
    default_settings = {'oauth_callback_urls': [CLAUDE_CALLBACK]}

    def init(self):
        from indico.modules.oauth.controllers import RHOAuthAuthorize, RHOAuthMetadata, RHOAuthToken

        from indico_mcp import oauth

        super().init()
        self.connect(signals.core.app_created, self._configure_app)
        self.connect(signals.rh.process, oauth.add_registration_endpoint, sender=RHOAuthMetadata)
        self.connect(signals.rh.before_process, oauth.authorize_registered_client, sender=RHOAuthAuthorize)
        self.connect(signals.rh.before_process, oauth.validate_registered_client_token_request, sender=RHOAuthToken)
        self.template_hook('plugin-details', self._render_setup)

    def _render_setup(self, plugin, **kwargs):
        if plugin.name == self.name:
            from indico_mcp.admin import render_setup

            return render_setup()

    def get_blueprints(self):
        from indico_mcp.endpoint import blueprint

        return blueprint

    def _configure_app(self, app):
        from indico_mcp import core_actions, event_extensions, navigation, registrations, room_booking, stsa_extensions

        scopes = {
            READ_SCOPE: _('Read Indico data through MCP'),
            WRITE_SCOPE: _('Perform Indico actions through MCP'),
        }
        SCOPES.update(scopes)
        supported_scopes = list(SCOPES)
        app.config['OAUTH2_SCOPES_SUPPORTED'] = auth_server.scopes_supported = supported_scopes
        # Indico 3.3 snapshots the choices when declaring these WTForms fields.
        choices = sorted(SCOPES.items(), key=itemgetter(1))
        ApplicationForm.allowed_scopes.kwargs['choices'] = choices
        PersonalTokenForm.scopes.kwargs['choices'] = choices

        registry = Registry()
        active_plugins = plugin_engine.get_active_plugins()
        core_actions.register_tools(registry)
        navigation.register_tools(registry)
        registrations.register_tools(registry)
        if config.ENABLE_ROOMBOOKING:
            room_booking.register_tools(registry)
        event_extensions.register_tools(registry, active_plugins)
        stsa_extensions.register_tools(registry, active_plugins)
        app.extensions[EXTENSION_NAME] = registry
