from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    app_profile: str = "evaluation"
    outbound_mode: str = "disabled"
    chatwoot_write_enabled: bool = False
    live_reply_account_id: int = 180474
    live_reply_inbox_id: int = 128859
    ai_engine_default: str = "v3"
    live_sop_enabled: bool = False
    live_sop_scope: str = "allowlist"
    live_sop_conversation_ids: str = ""
    public_api_base_url: str = "https://api.luoxuecong.asia"
    app_secret_key: str = "development-only-change-me"
    app_encryption_key: str = ""
    database_url: str = "sqlite:///./data/app.db"
    frontend_origins: str = "http://127.0.0.1:5173,http://127.0.0.1:4175"
    session_cookie_domain: str = ""
    session_cookie_secure: bool = False
    session_ttl_hours: int = 12
    chatwoot_request_timeout_seconds: int = 15
    ai_request_timeout_seconds: int = 15
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"
    deepseek_timeout_seconds: int = 20
    deepseek_max_input_characters: int = 300000
    evaluation_poll_interval_seconds: float = 1
    evaluation_inbox_id: int = 128859
    business_materials_dir: str = r"E:\AI\codexproject\ai_chatwoot\給ai"
    worker_poll_interval_seconds: float = 1
    worker_lease_seconds: int = 60
    live_reply_concurrency: int = Field(default=4, ge=1, le=8)
    worker_stale_seconds: int = Field(default=120, ge=30, le=600)
    relay_base_url: str = ""
    relay_api_token: str = ""
    relay_poll_interval_seconds: float = 1
    relay_poll_batch_size: int = 10
    relay_request_timeout_seconds: int = 15
    relay_cleanup_interval_seconds: int = 21600
    upload_dir: str = "./data/uploads"
    log_level: str = "INFO"

    @property
    def origins(self) -> list[str]:
        return [value.strip() for value in self.frontend_origins.split(",") if value.strip()]

    @property
    def relay_enabled(self) -> bool:
        return bool(self.relay_base_url and self.relay_api_token)

    @property
    def outbound_enabled(self) -> bool:
        return self.outbound_mode == "live" and self.chatwoot_write_enabled

    @property
    def live_sop_allowlist(self) -> set[int]:
        values: set[int] = set()
        for item in self.live_sop_conversation_ids.replace("，", ",").split(","):
            item = item.strip()
            if item:
                values.add(int(item))
        return values

    def validate_runtime(self) -> None:
        if self.outbound_mode not in {"disabled", "record_only", "live"}:
            raise ValueError("invalid_outbound_mode")
        if self.live_sop_scope not in {"allowlist", "ai_label"}:
            raise ValueError("invalid_live_sop_scope")
        self.ai_engine_default = "v3"
        if self.app_profile == "evaluation" and self.outbound_enabled:
            raise ValueError("evaluation_profile_cannot_enable_outbound")
        if self.live_sop_enabled and not self.outbound_enabled:
            raise ValueError("live_sop_requires_live_outbound")
        if self.live_sop_enabled and self.live_sop_scope == "allowlist" and not self.live_sop_allowlist:
            raise ValueError("live_sop_allowlist_scope_requires_allowlist")

    def ensure_directories(self) -> None:
        if self.database_url.startswith("sqlite:///./"):
            Path(self.database_url.removeprefix("sqlite:///./")).parent.mkdir(parents=True, exist_ok=True)
        Path(self.upload_dir).mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    value = Settings()
    value.validate_runtime()
    value.ensure_directories()
    return value


settings = get_settings()
