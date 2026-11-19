"""Tools for Indico material folders, attachment metadata, and links."""

from typing import Annotated, Literal
from urllib.parse import urlsplit

from indico.core import signals
from indico.core.db import db
from indico.modules.attachments import logger
from indico.modules.attachments.forms import AddAttachmentLinkForm
from indico.modules.attachments.models.attachments import Attachment, AttachmentType
from indico.modules.attachments.models.folders import AttachmentFolder
from indico.modules.attachments.operations import add_attachment_link
from indico.modules.attachments.util import can_manage_attachments
from pydantic import BaseModel, ConfigDict, Field
from werkzeug.datastructures import MultiDict
from werkzeug.exceptions import BadRequest, Forbidden

from indico_mcp.core import (
    CategoryID,
    Description,
    ObjectID,
    PageSize,
    Title,
    get_category,
    get_event,
    get_page,
    require_access,
    require_confirmation,
    require_unlocked,
)
from indico_mcp.core_schedule import get_contribution, get_session, get_subcontribution, require_visible_contribution

Link = Annotated[str, Field(min_length=1, max_length=2048, description='Absolute HTTP or HTTPS URL.')]


class MaterialTarget(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    kind: Literal['category', 'event', 'session', 'contribution', 'subcontribution']
    id: CategoryID = Field(description='The selected object ID. Use 0 for the root category.')
    event_id: ObjectID | None = Field(default=None, description='Parent event for a session or contribution.')
    contribution_id: ObjectID | None = Field(default=None, description='Required for a subcontribution.')


def _get_target(target):
    if target.kind == 'category':
        return get_category(target.id)
    if target.kind == 'event':
        return get_event(target.id)
    if target.event_id is None:
        raise BadRequest('Supply the parent event_id for this material target.')
    event = get_event(target.event_id)
    if target.kind == 'session':
        return get_session(event, target.id)
    if target.kind == 'contribution':
        return get_contribution(event, target.id)
    if target.contribution_id is None:
        raise BadRequest('Supply the parent contribution_id for a subcontribution.')
    return get_subcontribution(get_contribution(event, target.contribution_id), target.id)


def _require_manage_materials(target, user):
    obj = _get_target(target)
    if not can_manage_attachments(obj, user):
        raise Forbidden('You do not have permission to manage these materials.')
    if target.kind != 'category':
        require_unlocked(obj.event)
    return obj


def _require_view_materials(target, user):
    obj = _get_target(target)
    if can_manage_attachments(obj, user):
        return obj
    require_access(obj, user)
    if target.kind == 'contribution':
        require_visible_contribution(obj, user)
    if target.kind == 'subcontribution':
        require_visible_contribution(obj.contribution, user)
    return obj


def _get_folder(obj, folder_id):
    return obj.attachment_folders.filter_by(id=folder_id, is_deleted=False).first_or_404()


def _get_attachment(obj, attachment_id):
    folder_ids = obj.attachment_folders.filter_by(is_deleted=False).with_entities(AttachmentFolder.id)
    return Attachment.query.filter(Attachment.folder_id.in_(folder_ids)).filter_by(id=attachment_id,
                                                                                   is_deleted=False).first_or_404()


def _serialize_folder(folder):
    return {'id': folder.id, 'title': folder.title, 'description': folder.description,
            'is_default': folder.is_default, 'is_hidden': folder.is_hidden}


def _serialize_attachment(attachment):
    result = {'id': attachment.id, 'title': attachment.title, 'description': attachment.description,
              'type': attachment.type.name, 'folder_id': attachment.folder_id,
              'download_url': attachment.absolute_download_url}
    if attachment.type == AttachmentType.file:
        result.update(filename=attachment.file.filename, content_type=attachment.file.content_type)
    return result


def _validate_link(value):
    try:
        url = urlsplit(value)
        if url.scheme in {'https', 'http'} and url.hostname and url.username is None and not any(
                char.isspace() or ord(char) < 32 for char in value):
            return value
    except ValueError:
        pass
    raise BadRequest('Use an absolute HTTP or HTTPS URL without credentials.')


def register_tools(registry):
    @registry.tool()
    def indico_list_material_folders(user, target: MaterialTarget, after_id: CategoryID = 0, limit: PageSize = 30):
        """List accessible material folders attached to a category, event, session, or contribution."""
        obj = _require_view_materials(target, user)
        manage = can_manage_attachments(obj, user)
        query = obj.attachment_folders.filter_by(is_deleted=False)
        return get_page(query, AttachmentFolder, after_id, limit,
                        lambda folder: manage or (folder.can_view(user) and folder.can_access(user)), _serialize_folder)

    @registry.tool()
    def indico_list_materials(user, target: MaterialTarget, folder_id: ObjectID | None = None,
                              after_id: CategoryID = 0, limit: PageSize = 30):
        """List accessible attachment metadata and native download links. Optionally filter by folder."""
        obj = _require_view_materials(target, user)
        manage = can_manage_attachments(obj, user)
        folders = obj.attachment_folders.filter_by(is_deleted=False)
        if folder_id is not None:
            folders = folders.filter_by(id=folder_id)
        visible_folders = [folder.id for folder in folders
                           if manage or (folder.can_view(user) and folder.can_access(user))]
        query = Attachment.query.filter(Attachment.folder_id.in_(visible_folders)).filter_by(is_deleted=False)
        return get_page(query, Attachment, after_id, limit, lambda item: item.can_access(user), _serialize_attachment)

    @registry.tool()
    def indico_get_material(user, target: MaterialTarget, attachment_id: ObjectID):
        """Get attachment metadata and its protected Indico download link."""
        obj = _require_view_materials(target, user)
        attachment = _get_attachment(obj, attachment_id)
        require_access(attachment, user)
        return _serialize_attachment(attachment)

    @registry.tool(read_only=False)
    def indico_create_material_folder(user, target: MaterialTarget, title: Title, description: Description = ''):
        """Create a material folder that inherits access permissions from the selected object."""
        obj = _require_manage_materials(target, user)
        folder = AttachmentFolder(object=obj, title=title, description=description, is_always_visible=True)
        db.session.add(folder)
        db.session.flush()
        logger.info('Folder %s created by %s', folder, user)
        signals.attachments.folder_created.send(folder, user=user)
        return _serialize_folder(folder)

    @registry.tool(read_only=False, idempotent=True)
    def indico_update_material_folder(user, target: MaterialTarget, folder_id: ObjectID, title: Title | None = None,
                                      description: Description | None = None, hidden: bool | None = None):
        """Edit a named material folder's title, description, or hidden state while preserving its access rules."""
        obj = _require_manage_materials(target, user)
        folder = _get_folder(obj, folder_id)
        if folder.is_default:
            raise BadRequest('Create a named folder to edit folder properties.')
        if title is not None:
            folder.title = title
        if description is not None:
            folder.description = description
        if hidden is not None:
            folder.is_hidden = hidden
            if hidden:
                folder.is_always_visible = False
        logger.info('Folder %s edited by %s', folder, user)
        signals.attachments.folder_updated.send(folder, user=user)
        return _serialize_folder(folder)

    @registry.tool(read_only=False, destructive=True)
    def indico_delete_material_folder(user, target: MaterialTarget, folder_id: ObjectID, confirm: bool):
        """Delete a named material folder and remove its attachments from view."""
        require_confirmation(confirm)
        obj = _require_manage_materials(target, user)
        folder = _get_folder(obj, folder_id)
        if folder.is_default:
            raise BadRequest('Delete individual attachments from the default folder.')
        folder.is_deleted = True
        logger.info('Folder %s deleted by %s', folder, user)
        signals.attachments.folder_deleted.send(folder, user=user)
        return {'id': folder.id, 'deleted': True}

    @registry.tool(read_only=False)
    def indico_add_material_link(user, target: MaterialTarget, title: Title, url: Link, description: Description = '',
                                 folder_id: ObjectID | None = None):
        """Attach a link to an object or one of its material folders. The link inherits access permissions."""
        obj = _require_manage_materials(target, user)
        data = {'title': title, 'link_url': _validate_link(url), 'description': description}
        if folder_id is not None:
            data['folder'] = str(_get_folder(obj, folder_id).id)
        form = AddAttachmentLinkForm(formdata=MultiDict(data), linked_object=obj, csrf_enabled=False)
        if not form.validate():
            raise BadRequest('Invalid attachment fields: ' + ', '.join(sorted(form.errors)))
        add_attachment_link(form, obj)
        return {'added': True, 'title': title, 'folder_id': folder_id}

    @registry.tool(read_only=False, idempotent=True)
    def indico_update_material(user, target: MaterialTarget, attachment_id: ObjectID, title: Title | None = None,
                                description: Description | None = None, url: Link | None = None):
        """Edit a material title or description. A link attachment also accepts a replacement URL."""
        obj = _require_manage_materials(target, user)
        attachment = _get_attachment(obj, attachment_id)
        if url is not None:
            if attachment.type != AttachmentType.link:
                raise BadRequest('Only a link attachment has a URL to replace.')
            attachment.link_url = _validate_link(url)
        if title is not None:
            attachment.title = title
        if description is not None:
            attachment.description = description
        logger.info('Attachment %s edited by %s', attachment, user)
        signals.attachments.attachment_updated.send(attachment, user=user)
        return _serialize_attachment(attachment)

    @registry.tool(read_only=False, destructive=True)
    def indico_delete_material(user, target: MaterialTarget, attachment_id: ObjectID, confirm: bool):
        """Delete a file or link attachment from an object whose materials you manage."""
        require_confirmation(confirm)
        obj = _require_manage_materials(target, user)
        attachment = _get_attachment(obj, attachment_id)
        attachment.is_deleted = True
        logger.info('Attachment %s deleted by %s', attachment, user)
        signals.attachments.attachment_deleted.send(attachment, user=user)
        return {'id': attachment.id, 'deleted': True}
