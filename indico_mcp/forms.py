from urllib.parse import urlsplit

from indico.util.i18n import _
from indico.web.forms.base import IndicoForm
from indico.web.forms.fields import TextListField
from wtforms import SelectField
from wtforms.validators import ValidationError

CLAUDE_CALLBACK = 'https://claude.ai/api/mcp/auth_callback'


class CallbackURLsField(TextListField):
    def _validate_item(self, value):
        try:
            url = urlsplit(value)
            port = url.port
            if (url.scheme == 'https' and url.hostname and '@' not in url.netloc
                    and '#' not in value and '\\' not in value and len(value) <= 2048
                    and not any(ord(c) <= 32 or ord(c) == 127 for c in value) and (port is None or port > 0)):
                return
        except ValueError:
            pass
        raise ValidationError(_('Enter an exact HTTPS callback URL without credentials or a fragment.'))


class OAuthSettingsForm(IndicoForm):
    oauth_default_access = SelectField(
        _('Default access for automatic OAuth'),
        choices=[('read-only', _('Read-only')), ('read-write', _('Read/write'))],
        description=_('Choose the access new connectors request by default. Users approve the scopes in Indico.'),
    )
    oauth_callback_urls = CallbackURLsField(
        _('Automatic registration callback URLs'),
        description=_('Add one exact HTTPS callback URL per line. Leave empty to disable automatic registration.'),
    )
