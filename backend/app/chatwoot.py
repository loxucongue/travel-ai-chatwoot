import time
import json
from urllib.parse import urljoin

import httpx

from app.config import settings
from app.outbound_control import global_message_sending_enabled


class ChatwootError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 502):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class ChatwootClient:
    def __init__(self, base_url: str, account_id: int, token: str, timeout: int = 15):
        self.base_url = base_url.rstrip("/")
        self.account_id = account_id
        self.client = httpx.Client(
            timeout=timeout,
            headers={"api_access_token": token, "Accept": "application/json"},
            event_hooks={"request": [self._guard_request]},
        )

    def _check_method(self, method: str) -> None:
        if method.upper() not in {"GET", "HEAD"} and not settings.outbound_enabled:
            raise ChatwootError("chatwoot_write_blocked", "Chatwoot write requests are disabled", 403)

    def _guard_request(self, request: httpx.Request) -> None:
        self._check_method(request.method)
        # A strictly private JSON note cannot reach the customer. It remains
        # subject to environment/account write guards, but not the public-send switch.
        private_note = False
        if request.method == "POST" and request.headers.get("content-type", "").startswith("application/json"):
            try:
                body = json.loads(request.content)
                private_note = isinstance(body, dict) and body.get("private") is True
            except (ValueError, UnicodeError):
                pass
        if (request.method not in {"GET", "HEAD"}
                and request.url.path.endswith("/messages")
                and not private_note
                and not global_message_sending_enabled()):
            raise ChatwootError(
                "global_message_sending_disabled",
                "Global customer message sending is disabled",
                403,
            )
        if settings.app_profile == "live_reply" and request.method not in {"GET", "HEAD"}:
            import re
            prefix = f"/api/v1/accounts/{settings.live_reply_account_id}/conversations/"
            allowed_path = bool(re.fullmatch(re.escape(prefix) + r"\d+/(?:messages|labels|assignments)", request.url.path))
            allowed_path = allowed_path or request.url.path == f"/api/v1/accounts/{settings.live_reply_account_id}/labels"
            if (self.account_id != settings.live_reply_account_id or request.method != "POST"
                    or not allowed_path):
                raise ChatwootError("live_reply_write_scope_blocked", "Only replies and manual conversation controls are enabled", 403)

    def close(self) -> None:
        self.client.close()

    def _url(self, path: str) -> str:
        return f"{self.base_url}/api/v1/accounts/{self.account_id}/{path.lstrip('/')}"

    def request(self, method: str, path: str, **kwargs):
        method = method.upper()
        self._check_method(method)
        response = None
        for attempt in range(3):
            try:
                response = self.client.request(method, self._url(path), **kwargs)
            except httpx.HTTPError as exc:
                if method in {"GET", "HEAD"} and attempt < 2:
                    time.sleep(0.5 * (2 ** attempt))
                    continue
                raise ChatwootError("chatwoot_unreachable", type(exc).__name__) from exc
            if method in {"GET", "HEAD"} and (response.status_code == 429 or response.status_code >= 500) and attempt < 2:
                retry_after = response.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.replace(".", "", 1).isdigit() else 0.5 * (2 ** attempt)
                time.sleep(min(delay, 10))
                continue
            break
        assert response is not None
        if response.status_code >= 400:
            code = "chatwoot_unauthorized" if response.status_code in (401, 403) else "chatwoot_request_failed"
            raise ChatwootError(code, f"Chatwoot returned {response.status_code}", response.status_code)
        return response.json() if response.content else None

    def list_inboxes(self):
        return self.request("GET", "inboxes")

    def list_agents(self):
        return self.request("GET", "agents")

    def list_inbox_agents(self, inbox_id: int):
        return self.request("GET", f"inbox_members/{inbox_id}")

    def list_teams(self):
        return self.request("GET", "teams")

    def list_labels(self):
        return self.request("GET", "labels")

    def create_label(self, title: str, description: str, color: str, show_on_sidebar: bool):
        return self.request(
            "POST",
            "labels",
            json={
                "label": {
                    "title": title,
                    "description": description,
                    "color": color,
                    "show_on_sidebar": show_on_sidebar,
                }
            },
        )

    def list_webhooks(self):
        return self.request("GET", "webhooks")

    def create_webhook(self, url: str, events: list[str]):
        return self.request("POST", "webhooks", json={"webhook": {"url": url, "name": "China2Go AI Event Gateway", "subscriptions": events}})

    def update_webhook(self, webhook_id: int, url: str, events: list[str]):
        return self.request("PATCH", f"webhooks/{webhook_id}", json={"webhook": {"url": url, "name": "China2Go AI Event Gateway", "subscriptions": events}})

    def get_conversation(self, conversation_id: int):
        return self.request("GET", f"conversations/{conversation_id}")

    def list_conversations(self, page: int = 1, inbox_id: int | None = None):
        params = {"status": "all", "assignee_type": "all", "page": page}
        if inbox_id is not None:
            params["inbox_id"] = inbox_id
        return self.request("GET", "conversations", params=params)

    def get_conversation_labels(self, conversation_id: int):
        return self.request("GET", f"conversations/{conversation_id}/labels")

    def get_contact_labels(self, contact_id: int):
        return self.request("GET", f"contacts/{contact_id}/labels")

    def list_contact_conversations(self, contact_id: int):
        return self.request("GET", f"contacts/{contact_id}/conversations")

    def set_conversation_labels(self, conversation_id: int, labels: list[str]):
        return self.request("POST", f"conversations/{conversation_id}/labels", json={"labels": labels})

    def assign_conversation(self, conversation_id: int, assignee_id: int | None):
        return self.request(
            "POST",
            f"conversations/{conversation_id}/assignments",
            json={"assignee_id": assignee_id},
        )

    def get_messages(self, conversation_id: int, before: int | None = None):
        params = {"before": before} if before is not None else None
        return self.request("GET", f"conversations/{conversation_id}/messages", params=params)

    def create_text_message(self, conversation_id: int, content: str):
        return self.request("POST", f"conversations/{conversation_id}/messages", json={"content": content, "message_type": "outgoing", "private": False})

    def create_private_notification(self, conversation_id: int, content: str, bot_id: int):
        return self.request("POST", f"conversations/{conversation_id}/messages", json={
            "content": content, "message_type": "outgoing", "private": True,
            "content_type": "text", "sender_type": "AgentBot", "sender_id": bot_id,
        })

    def create_input_select_message(self, conversation_id: int, content: str, options: list[str]):
        items = [{"title": title, "value": title} for title in options]
        return self.request(
            "POST",
            f"conversations/{conversation_id}/messages",
            json={
                "content": content,
                "message_type": "outgoing",
                "private": False,
                "content_type": "input_select",
                "content_attributes": {"items": items},
            },
        )

    def create_attachment_message(self, conversation_id: int, content: str, path: str, mime_type: str):
        with open(path, "rb") as stream:
            return self.request(
                "POST",
                f"conversations/{conversation_id}/messages",
                data={"content": content, "message_type": "outgoing", "private": "false"},
                files={"attachments[]": (path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1], stream, mime_type)},
            )


def normalize_collection(value) -> list[dict]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("payload", "data"):
            if isinstance(value.get(key), list):
                return value[key]
    return []


class ReadOnlyChatwootClient(ChatwootClient):
    """Chatwoot client with a transport-level read-only guarantee."""

    def _check_method(self, method: str) -> None:
        if method.upper() not in {"GET", "HEAD"}:
            raise ChatwootError("chatwoot_write_blocked", "Read-only Chatwoot client rejected a write request", 403)

    def create_label(self, *args, **kwargs):
        raise ChatwootError("chatwoot_write_blocked", "Read-only Chatwoot client", 403)

    def create_webhook(self, *args, **kwargs):
        raise ChatwootError("chatwoot_write_blocked", "Read-only Chatwoot client", 403)

    def update_webhook(self, *args, **kwargs):
        raise ChatwootError("chatwoot_write_blocked", "Read-only Chatwoot client", 403)

    def set_conversation_labels(self, *args, **kwargs):
        raise ChatwootError("chatwoot_write_blocked", "Read-only Chatwoot client", 403)

    def assign_conversation(self, *args, **kwargs):
        raise ChatwootError("chatwoot_write_blocked", "Read-only Chatwoot client", 403)

    def create_text_message(self, *args, **kwargs):
        raise ChatwootError("chatwoot_write_blocked", "Read-only Chatwoot client", 403)

    def create_input_select_message(self, *args, **kwargs):
        raise ChatwootError("chatwoot_write_blocked", "Read-only Chatwoot client", 403)

    def create_attachment_message(self, *args, **kwargs):
        raise ChatwootError("chatwoot_write_blocked", "Read-only Chatwoot client", 403)
