"""Single configuration contract consumed by V3 and its delivery scheduler."""
from copy import deepcopy
from typing import Literal
from pydantic import BaseModel,Field,model_validator
from app.operations import save_setting,setting_value
from app.route_packages import ROUTE_PACKAGES
from app.opening_messages import OpeningItem,delivery_items,opening_media_info
from app.config import settings
SETTING_KEY="route_reception_config"
SUPPORTED_CONTACT_CHANNELS=["LINE","微信","电话","Email","WhatsApp"]
DEFAULT_TONE_GUIDANCE="像台灣旅遊顧問自然聊天，優先使用配置原話，按客戶當前問題選段。"


class ReplySettings(BaseModel):
    opening_items: list[OpeningItem] | None = Field(default=None, min_length=1, max_length=10)
    opening_messages: list[str] | None = Field(default=None, min_length=1, max_length=10)
    opening_interval_seconds: int = Field(default=2, ge=1, le=30)
    opening_message: str = Field(default="您好～這裡是 China2Go 國旅環球，您想先了解哪一條行程呢？", min_length=1, max_length=200)
    goal: str = Field(default="先回答客户，再自然取得一种有效联系方式", min_length=1, max_length=160)
    tone: Literal["friendly_professional", "concise", "warm"] = "friendly_professional"
    tone_guidance: str = Field(default=DEFAULT_TONE_GUIDANCE, max_length=1200)
    opening_character_limit: int = Field(default=200, ge=80, le=200)
    custom_guidance: str = Field(default="", max_length=40000)

    @model_validator(mode="before")
    @classmethod
    def typed_opening_owns_legacy_fields(cls, value):
        if isinstance(value, dict) and value.get("opening_items") is not None:
            value = dict(value)
            value.pop("opening_message", None)
            value.pop("opening_messages", None)
        return value

    @model_validator(mode="after")
    def validate_opening(self):
        self.opening_message = self.opening_message.strip()
        if self.opening_items is not None:
            if len({item.key for item in self.opening_items}) != len(self.opening_items):
                raise ValueError("opening_message_key_duplicate")
            if any(len(item.content) > self.opening_character_limit for item in self.opening_items):
                raise ValueError("opening_message_exceeds_reply_limit")
            items = delivery_items([item.model_dump() for item in self.opening_items], [])
            # The authored list is the entire delivery list; no generated tail.
            self.opening_items = [OpeningItem.model_validate(item) for item in items]
            self.opening_messages = [item["content"] for item in items if item["content"]]
            self.opening_message = self.opening_messages[0] if self.opening_messages else ''
            return self
        messages = self.opening_messages if self.opening_messages is not None else [self.opening_message]
        messages = [item.strip() for item in messages]
        if any(not item or len(item) > self.opening_character_limit for item in messages):
            raise ValueError("opening_message_exceeds_reply_limit")
        self.opening_messages = messages
        self.opening_message = messages[0]
        return self


class LeadSettings(BaseModel):
    enabled: bool = True
    channels: list[Literal["LINE", "微信", "电话", "Email", "WhatsApp"]] = Field(
        default_factory=lambda: list(SUPPORTED_CONTACT_CHANNELS), min_length=1, max_length=5
    )
    require_supported_route: bool = True
    require_party_size: bool = False
    require_departure_window: bool = False


class RoutingSettings(BaseModel):
    enabled_route_variants: list[str] = Field(default_factory=lambda: list(ROUTE_PACKAGES), min_length=1)
    allow_route_switch: bool = True
    preserve_profile_on_switch: bool = True
    outside_catalog_action: Literal["recommend_supported_routes", "explain_boundary_only", "consult_advisor"] = "consult_advisor"

    @model_validator(mode="after")
    def supported_routes_only(self):
        unknown = set(self.enabled_route_variants) - set(ROUTE_PACKAGES)
        if unknown:
            raise ValueError(f"unsupported_route_variants:{','.join(sorted(unknown))}")
        self.enabled_route_variants = list(dict.fromkeys(self.enabled_route_variants))
        return self


class HandoffSettings(BaseModel):
    large_group_enabled: bool = True
    large_group_minimum: int = Field(default=8, ge=2, le=100)


class CommonScript(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=100)
    scenario: str = Field(min_length=1, max_length=1000)
    text: str = Field(min_length=1, max_length=10000)
    enabled: bool = True



class SilenceSettings(BaseModel):
    enabled: bool = True
    live_enabled: bool = False
    intervals_minutes: list[int] = Field(default_factory=lambda:[1,120],min_length=1,max_length=20)
    active_start: str = "09:00"
    active_end: str = "21:00"
    max_proactive_messages_per_day: int = Field(default=6,ge=1,le=20)

    @model_validator(mode="after")
    def valid(self):
        from datetime import time
        time.fromisoformat(self.active_start);time.fromisoformat(self.active_end)
        if any(x<1 or x>1440 for x in self.intervals_minutes):
            raise ValueError("silence_interval_out_of_range")
        return self

class ReceptionConfiguration(BaseModel):
    schema_version: int = 7
    reply: ReplySettings = Field(default_factory=ReplySettings)
    lead_capture: LeadSettings = Field(default_factory=LeadSettings)
    routing: RoutingSettings = Field(default_factory=RoutingSettings)
    handoff: HandoffSettings = Field(default_factory=HandoffSettings)
    silence: SilenceSettings = Field(default_factory=SilenceSettings)
    common_scripts: list[CommonScript] = Field(default_factory=list,max_length=100)

def default_reception_configuration():
    return ReceptionConfiguration().model_dump()

def get_reception_configuration(db):
    value=deepcopy(setting_value(db,SETTING_KEY,default_reception_configuration()))
    # One-time stored-data compatibility; no legacy engine or execution path.
    if value.get('schema_version',0)<7:
        silence=value.setdefault('silence',{})
        if silence.get('v2_intervals_minutes'):
            silence['intervals_minutes']=silence.pop('v2_intervals_minutes')
        if silence.get('live_enabled') is None:
            silence['live_enabled']=settings.live_sop_enabled
    value['schema_version']=7
    return ReceptionConfiguration.model_validate(value).model_dump()

def put_reception_configuration(db,payload):
    value=payload.model_dump()
    from app.models import Tenant
    from sqlalchemy import select
    tenant=db.scalar(select(Tenant).order_by(Tenant.id))
    for item in value['reply'].get('opening_items') or []:
        if item['content_type']!='text':
            info=opening_media_info(db,item,tenant.id if tenant else None)
            item['media_hash']=info['media_hash']
    save_setting(db,SETTING_KEY,value)
    return value

def live_silence_enabled(db):
    value=get_reception_configuration(db)['silence']
    return value['enabled'] and value['live_enabled']
