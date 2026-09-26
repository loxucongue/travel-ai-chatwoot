from dataclasses import dataclass

import httpx

from app.models import AppSetting
from app.operations import setting_value
from app.security import decrypt_secret


@dataclass(frozen=True)
class AiDecision:
    action: str
    reply: str | None = None
    handoff_reason: str | None = None
    stage: str | None = None
    captured_contacts: list | None = None


def decide(db, context: dict) -> AiDecision:
    config = setting_value(db, "ai_adapter", {"adapter": "mock", "enabled": True, "timeout_seconds": 15})
    if not config.get("enabled", True):
        return AiDecision("no_action")
    if config.get("adapter") != "http":
        mock = db.get(AppSetting, "mock_ai")
        value = mock.value if mock else {"trigger_text": "测试人员触发消息", "reply_text": "测试人员回复消息"}
        return AiDecision("reply", value.get("reply_text")) if context["message"]["content"].strip() == value.get("trigger_text") else AiDecision("no_action")
    if not config.get("url"):
        raise ValueError("ai_adapter_url_missing")
    headers = {"Content-Type": "application/json"}
    if config.get("encrypted_token"):
        headers["Authorization"] = f"Bearer {decrypt_secret(config['encrypted_token'].encode())}"
    response = httpx.post(config["url"], json=context, headers=headers, timeout=int(config.get("timeout_seconds", 15)))
    response.raise_for_status()
    data = response.json()
    action = data.get("action")
    if action not in ("reply", "handoff", "no_action"):
        raise ValueError("ai_adapter_invalid_action")
    if action == "reply" and not data.get("reply"):
        raise ValueError("ai_adapter_reply_missing")
    return AiDecision(action, data.get("reply"), data.get("handoff_reason"), data.get("stage"), data.get("captured_contacts") or [])

