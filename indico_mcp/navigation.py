from urllib.parse import urljoin, urlsplit

from indico.core.config import config
from indico.web.menu import build_menu_structure
from werkzeug.exceptions import Forbidden

from indico_mcp.core import ObjectID, get_event


def _menu_links(menu_name, **kwargs):
    base_url = config.BASE_URL.rstrip('/') + '/'
    base = urlsplit(base_url)
    entries = build_menu_structure(menu_name, **kwargs)
    links = []
    for entry in entries:
        for item in entry.items if entry.is_section else (entry,):
            if item.disabled or not item.url:
                continue
            url = urljoin(base_url, item.url)
            target = urlsplit(url)
            if (target.scheme, target.netloc) == (base.scheme, base.netloc):
                links.append({'name': item.name, 'title': str(item.title), 'url': url,
                              'section': str(entry.title) if entry.is_section else None})
    return links


def register_tools(registry):
    @registry.tool(idempotent=True)
    def indico_get_event_management_links(user, event_id: ObjectID):
        """Get the event's Indico management pages visible to you for workflows that use the browser."""
        event = get_event(event_id)
        if not event.can_manage(user, permission='ANY'):
            raise Forbidden
        return {'links': _menu_links('event-management-sidemenu', event=event)}

    @registry.tool(idempotent=True)
    def indico_get_site_admin_links(user):
        """Get the Indico administration pages, including active plugin pages, for the signed-in browser."""
        if not user.is_admin:
            raise Forbidden
        return {'links': _menu_links('admin-sidemenu')}
