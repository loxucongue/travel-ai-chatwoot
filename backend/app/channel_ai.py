"""One settings operation for the existing inbox and reception-scope gates."""
import hashlib
import json

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy import select

from app.models import InboxBinding
from app.operations import audit
from app.outbound_control import global_message_sending_enabled
from app.reception_config import get_reception_configuration, put_reception_configuration, ReceptionConfiguration
from app.reception_rollout import reception_inbox_ids


class ChannelChange(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: int = Field(gt=0, strict=True)
    enabled: StrictBool


class ChannelSettingsChange(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: str = Field(min_length=64, max_length=64)
    changes: list[ChannelChange] = Field(min_length=1, max_length=100)


def channel_settings(db):
    scope = reception_inbox_ids(db)
    items = []
    for row in db.scalars(select(InboxBinding).order_by(InboxBinding.id)):
        items.append({'id': row.id, 'chatwoot_inbox_id': row.chatwoot_inbox_id,
                      'name': row.name, 'channel_type': row.channel_type, 'status': row.status,
                      'ai_enabled': row.ai_enabled, 'in_reception_scope': row.chatwoot_inbox_id in scope,
                      'enabled': row.ai_enabled and row.chatwoot_inbox_id in scope})
    revision = hashlib.sha256(json.dumps({'items': items, 'scope': scope}, sort_keys=True).encode()).hexdigest()
    return {'items': items, 'revision': revision, 'global_sending_enabled': global_message_sending_enabled(db)}


def save_channel_settings(db, user, payload):
    before = channel_settings(db)
    if before['revision'] != payload.revision:
        raise HTTPException(409, detail={'code': 'channel_settings_changed', 'message': '渠道配置已被修改，请重新读取后再保存。'})
    rows = {row.id: row for row in db.scalars(select(InboxBinding))}
    if len({c.id for c in payload.changes}) != len(payload.changes):
        raise HTTPException(422, detail={'code': 'duplicate_channel', 'message': '同一渠道不能重复提交。'})
    for change in payload.changes:
        if change.id not in rows:
            raise HTTPException(404, detail={'code': 'inbox_not_found', 'message': '渠道不存在，请重新同步。'})
        if change.enabled and rows[change.id].status != 'active':
            raise HTTPException(422, detail={'code': 'inbox_inactive', 'message': '请先恢复渠道连接，再开启 AI。'})
    # Validate all changes before writing either gate; one transaction also
    # retains unrelated reception fields, materials and untouched channels.
    scope = reception_inbox_ids(db)
    for change in payload.changes:
        row = rows[change.id]
        row.ai_enabled = change.enabled
        scope = [i for i in scope if i != row.chatwoot_inbox_id]
        if change.enabled:
            scope.append(row.chatwoot_inbox_id)
    config = get_reception_configuration(db)
    config['routing']['live_inbox_ids'] = scope
    put_reception_configuration(db, ReceptionConfiguration.model_validate(config))
    db.flush()
    after = channel_settings(db)
    audit(db, user, 'channel_ai.updated', 'app_setting', 'route_reception_config',
          {'before': before['items'], 'after': after['items']})
    db.commit()
    return after
