from typing import Literal

from pydantic import BaseModel, EmailStr, Field, HttpUrl, model_validator


class UserCreate(BaseModel):
    email: EmailStr
    display_name: str = Field(min_length=1, max_length=120)
    role: Literal["admin", "supervisor", "agent"]
    chatwoot_agent_id: int | None = None
    inbox_binding_ids: list[int] = Field(default_factory=list)


class UserUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    role: Literal["admin", "supervisor", "agent"] | None = None
    active: bool | None = None
    chatwoot_agent_id: int | None = None
    inbox_binding_ids: list[int] | None = None


class HandoffCreate(BaseModel):
    conversation_id: int
    reason_code: str = Field(default="manual", max_length=80)
    reason_detail: str = Field(default="", max_length=1000)
    priority: Literal["P1", "P2", "P3"] = "P2"


class HandoffVersion(BaseModel):
    version: int


class HandoffAssign(HandoffVersion):
    user_id: int


class LabelMappings(BaseModel):
    handoff_labels: list[str] = Field(default_factory=lambda: ["人工接管", "客诉"])
    contact_block_labels: list[str] = Field(default_factory=lambda: ["拒绝联系", "黑名单"])
    lead_labels: list[str] = Field(default_factory=lambda: ["已留资"])
    conversion_labels: list[str] = Field(default_factory=lambda: ["已成交"])
    stage_labels: list[str] = Field(default_factory=list)
    sop_whitelist_label: str = "SOP测试白名单"


class NotificationSettings(BaseModel):
    enabled: bool = False
    channel: Literal["webhook", "chatwoot"] = "webhook"
    agent_id: int | None = Field(default=None, gt=0)
    bot_id: int | None = Field(default=None, gt=0)
    url: HttpUrl | None = None
    secret: str | None = Field(default=None, min_length=8)
    event_types: list[str] = Field(default_factory=lambda: ["handoff.created", "handoff.overdue"])

    @model_validator(mode="after")
    def validate_destination(self):
        if self.enabled and self.channel == "webhook" and not self.url:
            raise ValueError("notification_webhook_url_required")
        if self.enabled and self.channel == "chatwoot" and (not self.agent_id or not self.bot_id):
            raise ValueError("notification_agent_and_bot_required")
        return self


class AiAdapterSettings(BaseModel):
    adapter: Literal["mock", "http"] = "mock"
    enabled: bool = True
    url: HttpUrl | None = None
    bearer_token: str | None = None
    timeout_seconds: int = Field(default=15, ge=2, le=120)
    failure_handoff_threshold: int = Field(default=3, ge=1, le=20)


class GlobalMessageSendingSettings(BaseModel):
    enabled: bool


class AiReceptionRolloutSettings(BaseModel):
    allowlist_enabled: bool = True
    conversation_ids: list[int] = Field(default_factory=list, max_length=200)
    confirm_ai_label_scope: bool = False


class SopMessage(BaseModel):
    key: str = Field(min_length=1, max_length=80)
    content_type: Literal["text", "image", "video", "audio", "file"] = "text"
    content: str = Field(default="", max_length=10000)
    media_id: int | None = Field(default=None, gt=0)
    media_name: str | None = Field(default=None, max_length=255)
    asset_key: str | None = Field(default=None, max_length=120)
    media_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class SopNode(BaseModel):
    key: str = Field(min_length=1, max_length=80)
    skip_if_materials_provided: bool = False
    schedule_type: Literal["relative", "fixed", "calendar_day"] = "relative"
    delay_minutes: int | None = Field(default=60, ge=0, le=525600)
    fixed_at: str | None = None
    basis: Literal["enrollment", "last_customer_reply", "previous_node", "customer_added"] = "enrollment"
    day_number: int = Field(default=1, ge=1, le=365)
    time_of_day: str = Field(default="10:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    messages: list[SopMessage] | None = Field(default=None, min_length=1, max_length=10)
    content_type: Literal["text", "image", "video", "audio", "file"] = "text"
    content: str = Field(default="", max_length=10000)
    media_id: int | None = None


class SopCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    trigger_type: Literal["manual", "label", "stage", "first_message"] = "manual"
    trigger_labels: list[str] = Field(default_factory=list)
    inbox_ids: list[int] = Field(default_factory=list)
    nodes: list[SopNode] = Field(default_factory=list, max_length=20)
    exit_labels: list[str] = Field(default_factory=list)
    stop_on_incoming: Literal[True] = True
    frequency_hours: int = Field(default=24, ge=24, le=720)
    route_variant: str = Field(default="", max_length=80)
    test_conversation_ids: list[int] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def normalize_conditions(self):
        self.trigger_type = "label" if self.trigger_type == "stage" else self.trigger_type
        self.trigger_labels = sorted({x.strip() for x in self.trigger_labels if x.strip()}) if self.trigger_type == "label" else []
        self.exit_labels = sorted({x.strip() for x in self.exit_labels if x.strip()})
        self.inbox_ids = sorted(set(self.inbox_ids))
        self.test_conversation_ids = sorted(set(self.test_conversation_ids))
        if any(x <= 0 for x in self.test_conversation_ids):
            raise ValueError("invalid_test_conversation_id")
        return self


class SopUpdate(SopCreate):
    expected_version: int | None = None
    dry_run: bool = True
    live_enabled: bool = False


class SopEnroll(BaseModel):
    conversation_ids: list[int] = Field(min_length=1, max_length=200)
    reenroll: bool = False
    request_key: str | None = Field(default=None, min_length=1, max_length=80)
    environment: Literal["playground", "shadow", "live_test"] = "playground"
    confirm_live_delivery: bool = False
    allow_repeat_delivery: bool = False
