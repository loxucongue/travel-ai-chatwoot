from pydantic import BaseModel, ConfigDict, EmailStr, Field, HttpUrl, field_validator


class LoginRequest(BaseModel):
    email: EmailStr
    # Login must validate the stored credential instead of enforcing the
    # password-creation policy. This also keeps legacy/admin-reset passwords
    # usable while new password changes retain their stronger minimum.
    password: str = Field(min_length=1, max_length=200)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=8, max_length=200)
    new_password: str = Field(min_length=10, max_length=200)


class ChatwootConfigRequest(BaseModel):
    base_url: HttpUrl
    account_id: int
    api_token: str | None = Field(default=None, min_length=8)


class InboxPolicyRequest(BaseModel):
    ai_enabled: bool


class WebhookConfigRequest(BaseModel):
    events: list[str]


class ConversationAssignmentRequest(BaseModel):
    assignee_id: int | None = Field(default=None, ge=1)
    pause_ai: bool = True


class ConversationLabelsRequest(BaseModel):
    labels: list[str] = Field(default_factory=list, max_length=50)
    base_labels: list[str] = Field(default_factory=list, max_length=50)


class ConversationAiModeRequest(BaseModel):
    enabled: bool


class ConversationQuickReplyRequest(BaseModel):
    content: str = Field(min_length=1, max_length=2000)
    options: list[str] = Field(min_length=1, max_length=13)
    request_key: str = Field(min_length=8, max_length=120, pattern=r"^[A-Za-z0-9._:-]+$")

    @field_validator("content")
    @classmethod
    def normalize_content(cls, value: str) -> str:
        return value.strip()

    @field_validator("options")
    @classmethod
    def normalize_options(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value or len(value) > 20 for value in normalized):
            raise ValueError("quick_reply_title_invalid")
        if len({value.casefold() for value in normalized}) != len(normalized):
            raise ValueError("quick_reply_title_duplicate")
        return normalized


class LabelCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    color: str = Field(default="#1F93FF", pattern=r"^#[0-9A-Fa-f]{6}$")
    show_on_sidebar: bool = True
