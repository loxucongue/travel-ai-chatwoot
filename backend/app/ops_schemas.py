from typing import Literal

from pydantic import BaseModel, EmailStr, Field, HttpUrl, PositiveInt, model_validator, field_validator


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
    channel: Literal["webhook", "chatwoot", "wecom", "email"] = "webhook"
    agent_id: int | None = Field(default=None, gt=0)
    bot_id: int | None = Field(default=None, gt=0)
    assign_on_handoff: bool = False
    handoff_notify_agent_ids: list[PositiveInt] = Field(default_factory=list, max_length=50)
    url: HttpUrl | None = None
    secret: str | None = Field(default=None, min_length=8)
    recipients: dict[int, str] = Field(default_factory=dict)
    corp_id: str = ''
    wecom_agent_id: int | None = Field(default=None, gt=0)
    smtp_host: str = ''
    smtp_port: int = Field(default=465, ge=1, le=65535)
    smtp_security: Literal['ssl', 'starttls'] = 'ssl'
    smtp_user: str = ''
    sender_email: EmailStr | None = None
    connection_secret: str | None = Field(default=None, min_length=1)
    event_types: list[str] = Field(default_factory=lambda: ["handoff.created", "handoff.overdue"])

    @field_validator('handoff_notify_agent_ids')
    @classmethod
    def unique_handoff_recipients(cls, value):
        return list(dict.fromkeys(value))

    @model_validator(mode="after")
    def validate_destination(self):
        if self.enabled and self.channel == "webhook" and not self.url:
            raise ValueError("notification_webhook_url_required")
        if self.enabled and self.channel == "chatwoot" and (not self.agent_id or not self.bot_id):
            raise ValueError("notification_agent_and_bot_required")
        if any(agent <= 0 or not target.strip() or any(c in target for c in ('\r','\n','|')) for agent,target in self.recipients.items()):
            raise ValueError('notification_recipient_invalid')
        if self.channel == 'email':
            from pydantic import TypeAdapter
            for target in self.recipients.values():
                TypeAdapter(EmailStr).validate_python(target)
        if self.enabled and self.channel in ('wecom', 'email') and not self.recipients:
            raise ValueError('notification_recipient_mapping_required')
        if self.enabled and self.channel == 'wecom' and (not self.corp_id or not self.wecom_agent_id):
            raise ValueError('notification_wecom_app_required')
        if self.enabled and self.channel == 'email' and (not self.smtp_host or not self.smtp_user or not self.sender_email):
            raise ValueError('notification_smtp_required')
        return self


class GlobalMessageSendingSettings(BaseModel):
    enabled: bool


class AiReceptionRolloutSettings(BaseModel):
    allowlist_enabled: bool = True
    conversation_ids: list[int] = Field(default_factory=list, max_length=200)
    confirm_ai_label_scope: bool = False
