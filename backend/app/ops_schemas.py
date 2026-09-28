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


class GlobalMessageSendingSettings(BaseModel):
    enabled: bool


class AiReceptionRolloutSettings(BaseModel):
    allowlist_enabled: bool = True
    conversation_ids: list[int] = Field(default_factory=list, max_length=200)
    confirm_ai_label_scope: bool = False
