"""Editable reception settings layered over the reviewed route policy.

The database setting contains operator-owned choices only. Delivery safety,
Chatwoot opt-in and idempotency remain code-owned invariants.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Literal

from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

from app.config import settings
from app.operations import save_setting, setting_value
from app.route_packages import JOURNEY_POLICY, ROUTE_PACKAGES
from app.opening_messages import OpeningItem, delivery_items, opening_media_info


SETTING_KEY = "route_reception_config"
MAX_SILENCE_TOUCHES = 20
DEFAULT_TONE_GUIDANCE = (
    "像台灣真人旅遊顧問在 LINE 或社群私訊中聊天：自然、柔和、有禮，短句為主；"
    "不要每句固定加語氣詞，也不要重複同一種開場、圖片介紹或收尾。"
)
SUPPORTED_PROFILE_FIELDS = [
    "destination", "party_size", "departure_window", "budget",
    "first_time_tibet", "permit_awareness", "concerns",
    "decision_status", "intent_level", "unresolved_question", "contact_status",
]
SUPPORTED_CONTACT_CHANNELS = ["LINE", "微信", "电话", "Email"]
SUPPORTED_JOURNEY_STAGES = [
    "route_selection", "needs_discovery", "value_building", "objection_handling",
    "contact_ready", "contact_requested", "considering", "captured", "handoff",
]
SUPPORTED_TOUCH_GOALS = [
    "route_choice", "collect_need", "build_value", "handle_objection",
    "request_contact", "contact_reminder", "soft_nurture",
]


def default_business_rules() -> list[dict]:
    """Small editable rule list; delivery safety remains outside this list."""
    return [
        {"id": "large_group", "name": "大团交给人工", "enabled": True,
         "condition": "客户明确同行人数达到配置的大团人数阈值", "action": "handoff",
         "guidance": "确认人数后说明由顾问继续制定安排，不承诺价格或余位。", "system_key": "large_group"},
        {"id": "captured_contact", "name": "取得联系方式后交给人工", "enabled": True,
         "condition": "客户提供了有效的 LINE、微信、电话或 Email", "action": "handoff",
         "guidance": "确认收到联系方式并说明顾问会继续跟进。", "system_key": "captured_contact"},
        {"id": "explicit_human", "name": "客户要求真人", "enabled": True,
         "condition": "客户明确要求真人客服或顾问接待", "action": "handoff",
         "guidance": "简短确认，停止 AI 继续营销。", "system_key": "explicit_human"},
        {"id": "service_dispute", "name": "售后争议交给人工", "enabled": True,
         "condition": "客户正在处理投诉、退款或合同争议", "action": "handoff",
         "guidance": "不承诺处理结果，由人工根据订单和条款继续处理。", "system_key": "service_dispute"},
        {"id": "attachment_review", "name": "必须查看附件", "enabled": True,
         "condition": "回答依赖客户本轮图片或文件的实际内容，当前无法可靠识别", "action": "handoff",
         "guidance": "说明需要顾问查看附件，不追加线路或留资问题。", "system_key": "attachment_review"},
    ]


class ReplySettings(BaseModel):
    opening_items: list[OpeningItem] | None = Field(default=None, min_length=1, max_length=10)
    opening_messages: list[str] | None = Field(default=None, min_length=1, max_length=10)
    opening_interval_seconds: int = Field(default=2, ge=1, le=30)
    opening_message: str = Field(default="您好～這裡是 China2Go 國旅環球，您想先了解哪一條行程呢？", min_length=1, max_length=200)
    goal: str = Field(default="先回答客户，再自然取得一种有效联系方式", min_length=1, max_length=160)
    tone: Literal["friendly_professional", "concise", "warm"] = "friendly_professional"
    tone_guidance: str = Field(default=DEFAULT_TONE_GUIDANCE, max_length=1200)
    max_characters: int = Field(default=200, ge=80, le=200)
    max_images_per_turn: int = Field(default=2, ge=0, le=2)
    custom_guidance: str = Field(default="", max_length=1200)

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
            if any(len(item.content) > self.max_characters for item in self.opening_items):
                raise ValueError("opening_message_exceeds_reply_limit")
            items = delivery_items([item.model_dump() for item in self.opening_items], [])
            self.opening_messages = [item["content"] for item in items if item["content"]]
            self.opening_message = self.opening_messages[0]
            return self
        messages = self.opening_messages if self.opening_messages is not None else [self.opening_message]
        messages = [item.strip() for item in messages]
        if any(not item or len(item) > self.max_characters for item in messages):
            raise ValueError("opening_message_exceeds_reply_limit")
        self.opening_messages = messages
        self.opening_message = messages[0]
        return self


class LeadSettings(BaseModel):
    enabled: bool = True
    channels: list[Literal["LINE", "微信", "电话", "Email"]] = Field(
        default_factory=lambda: list(SUPPORTED_CONTACT_CHANNELS), min_length=1, max_length=4
    )
    require_supported_route: bool = True
    require_party_size: bool = False
    require_departure_window: bool = False
    answer_before_asking: bool = True
    ask_after_answered_topics: int = Field(default=2, ge=1, le=5)


class RoutingSettings(BaseModel):
    enabled_route_variants: list[str] = Field(default_factory=lambda: list(ROUTE_PACKAGES), min_length=1)
    allow_route_switch: bool = True
    preserve_profile_on_switch: bool = True
    outside_catalog_action: Literal["recommend_supported_routes", "explain_boundary_only"] = "recommend_supported_routes"

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


class BusinessRule(BaseModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{3,64}$")
    name: str = Field(min_length=1, max_length=80)
    enabled: bool = True
    condition: str = Field(min_length=1, max_length=500)
    action: Literal["handoff", "request_contact", "recommend_routes", "continue_ai", "stop_ai"]
    guidance: str = Field(default="", max_length=500)
    trigger: Literal["semantic", "outside_catalog"] = "semantic"
    system_key: Literal[
        "large_group", "captured_contact", "explicit_human", "service_dispute", "attachment_review"
    ] | None = None


class SilenceSettings(BaseModel):
    v2_intervals_minutes: list[int] = Field(default_factory=lambda: [1, 120], min_length=1, max_length=20)
    enabled: bool = True
    intervals_minutes: list[int] = Field(
        default_factory=lambda: [1, 3, 5, 10, 30, 60],
        min_length=1,
        max_length=MAX_SILENCE_TOUCHES,
    )
    max_proactive_messages_per_day: int = Field(default=6, ge=1, le=MAX_SILENCE_TOUCHES)
    active_start: str = "09:00"
    active_end: str = "21:00"

    @model_validator(mode="after")
    def valid_intervals(self):
        if any(value < 1 or value > 1440 for value in self.v2_intervals_minutes):
            raise ValueError('v2_silence_interval_out_of_range')
        if sum(self.v2_intervals_minutes) >= 23 * 60 + 55:
            raise ValueError('v2_silence_schedule_exceeds_channel_window')
        if any(value < 1 or value > 1440 for value in self.intervals_minutes):
            raise ValueError("silence_interval_out_of_range")
        if self.intervals_minutes != sorted(set(self.intervals_minutes)):
            raise ValueError("silence_intervals_must_be_unique_and_ascending")
        if self.max_proactive_messages_per_day > len(self.intervals_minutes):
            raise ValueError("daily_limit_exceeds_timeline")
        for value in (self.active_start, self.active_end):
            parts = value.split(":")
            if len(parts) != 2 or not all(part.isdigit() for part in parts):
                raise ValueError("active_time_invalid")
            hour, minute = map(int, parts)
            if hour > 23 or minute > 59:
                raise ValueError("active_time_invalid")
        return self


class StageJourneySettings(BaseModel):
    mandatory_send: bool = False
    skip_when_no_relevant_content: bool = True
    model_max_attempts: int = Field(default=2, ge=1, le=3)
    model_failure_action: Literal["warn_and_skip_touch"] = "warn_and_skip_touch"
    stages: list[str] = Field(default_factory=lambda: list(SUPPORTED_JOURNEY_STAGES))
    touch_goals: list[str] = Field(default_factory=lambda: list(SUPPORTED_TOUCH_GOALS))

    @model_validator(mode="after")
    def supported_values_only(self):
        if set(self.stages) - set(SUPPORTED_JOURNEY_STAGES):
            raise ValueError("unsupported_journey_stage")
        if set(self.touch_goals) - set(SUPPORTED_TOUCH_GOALS):
            raise ValueError("unsupported_touch_goal")
        self.stages = list(dict.fromkeys(self.stages))
        self.touch_goals = list(dict.fromkeys(self.touch_goals))
        # Kept in the stored shape for old clients. The executable contract is
        # fixed: code may skip a touch with no new value, and a narrow model
        # node receives at most one schema-repair attempt.
        self.mandatory_send = False
        self.skip_when_no_relevant_content = True
        self.model_max_attempts = min(self.model_max_attempts, 2)
        return self


class ReceptionConfiguration(BaseModel):
    schema_version: int = 5
    reply: ReplySettings = Field(default_factory=ReplySettings)
    profile_fields: list[str] = Field(
        default_factory=lambda: list(SUPPORTED_PROFILE_FIELDS), min_length=1, max_length=11
    )
    lead_capture: LeadSettings = Field(default_factory=LeadSettings)
    routing: RoutingSettings = Field(default_factory=RoutingSettings)
    handoff: HandoffSettings = Field(default_factory=HandoffSettings)
    business_rules: list[BusinessRule] = Field(
        default_factory=lambda: [BusinessRule.model_validate(item) for item in default_business_rules()],
        max_length=30,
    )
    silence: SilenceSettings = Field(default_factory=SilenceSettings)
    stage_journey: StageJourneySettings = Field(default_factory=StageJourneySettings)

    @model_validator(mode="after")
    def normalize_lists(self):
        unknown = set(self.profile_fields) - set(SUPPORTED_PROFILE_FIELDS)
        if unknown:
            raise ValueError(f"unsupported_profile_fields:{','.join(sorted(unknown))}")
        self.profile_fields = list(dict.fromkeys(self.profile_fields))
        self.lead_capture.channels = list(dict.fromkeys(self.lead_capture.channels))
        ids = [rule.id for rule in self.business_rules]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate_business_rule_id")
        system_keys = [rule.system_key for rule in self.business_rules if rule.system_key]
        if len(system_keys) != len(set(system_keys)):
            raise ValueError("duplicate_business_rule_system_key")
        return self


def default_reception_configuration() -> dict:
    policy = JOURNEY_POLICY
    return ReceptionConfiguration(
        reply={
            "max_characters": policy["reply_style"]["max_characters"],
            "max_images_per_turn": min(2, policy["reply_style"]["max_images_per_turn"]),
        },
        routing={
            "enabled_route_variants": list(ROUTE_PACKAGES),
            "allow_route_switch": True,
            "preserve_profile_on_switch": True,
            "outside_catalog_action": "recommend_supported_routes",
        },
        handoff={
            "large_group_enabled": policy["handoff"]["large_group"]["enabled"],
            "large_group_minimum": policy["handoff"]["large_group"]["minimum_party_size"],
        },
        silence={
            "enabled": True,
            "intervals_minutes": [
                policy["silence_journey"]["mainline_after_minutes"],
                *policy["silence_journey"]["wakeup_after_minutes"],
            ],
            "max_proactive_messages_per_day": policy["silence_journey"]["max_proactive_messages_per_day"],
            "active_start": policy["safety"]["active_hours"]["start"],
            "active_end": policy["safety"]["active_hours"]["end"],
        },
    ).model_dump()


def get_reception_configuration(db: Session) -> dict:
    value = setting_value(db, SETTING_KEY, default_reception_configuration())
    # Persisted settings are intentionally forward-compatible.  The model
    # fills newly introduced sections from their defaults, while callers see
    # the current schema version and will save v5 on the next edit.
    value = deepcopy(value)
    value["schema_version"] = 5
    value.setdefault("reply", {})["max_images_per_turn"] = min(
        2, int(value.get("reply", {}).get("max_images_per_turn", 2))
    )
    return ReceptionConfiguration.model_validate(value).model_dump()


def put_reception_configuration(db: Session, payload: ReceptionConfiguration) -> dict:
    value = payload.model_dump()
    if value["reply"].get("opening_items"):
        from fastapi import HTTPException
        from sqlalchemy import select
        from app.models import Tenant

        tenant = db.scalar(select(Tenant).order_by(Tenant.id))
        hashes = set()
        try:
            for item in value["reply"]["opening_items"]:
                if item["content_type"] == "text":
                    continue
                info = opening_media_info(db, item, tenant.id if tenant else None)
                if info["media_hash"] in hashes:
                    raise ValueError("opening_media_duplicate")
                hashes.add(info["media_hash"])
                item.update({key: info[key] for key in ("media_hash", "media_name")})
        except ValueError as exc:
            raise HTTPException(422, detail={"code": str(exc), "message": "开场附件无效、重复或已变化，请检查文件后重新发布。"}) from exc
    save_setting(db, SETTING_KEY, value)
    return value


def effective_reception_policy(db: Session) -> dict:
    return policy_from_configuration(get_reception_configuration(db))


def policy_from_configuration(config: dict) -> dict:
    """Compile the same operator configuration for runtime and isolated acceptance."""
    policy = deepcopy(JOURNEY_POLICY)
    policy["reply_style"].update({
        "max_characters": config["reply"]["max_characters"],
        "max_images_per_turn": config["reply"]["max_images_per_turn"],
        "operator_tone": config["reply"]["tone"],
    })
    intervals = config["silence"]["intervals_minutes"]
    policy["silence_journey"].update({
        "enabled": config["silence"]["enabled"],
        "mainline_after_minutes": intervals[0],
        "mainline_max_touches": 1,
        "wakeup_after_minutes": intervals[1:],
        "wakeup_max_touches": max(0, len(intervals) - 1),
        "stop_after_minutes": sum(intervals),
        "max_proactive_messages_per_day": config["silence"]["max_proactive_messages_per_day"],
    })
    policy["handoff"]["large_group"].update({
        "enabled": config["handoff"]["large_group_enabled"],
        "minimum_party_size": config["handoff"]["large_group_minimum"],
        "source": "operator_reception_config",
    })
    policy["route_switch"].update({
        "enabled": config["routing"]["allow_route_switch"],
        "preserve_slots": config["profile_fields"] if config["routing"]["preserve_profile_on_switch"] else [],
        "allowed_routes": config["routing"]["enabled_route_variants"],
        "outside_catalog_action": config["routing"]["outside_catalog_action"],
    })
    policy["safety"]["active_hours"].update({
        "start": config["silence"]["active_start"],
        "end": config["silence"]["active_end"],
    })
    policy["safety"]["require_ai_label"] = True
    policy["operator_configuration"] = {
        "business_goal": config["reply"]["goal"],
        "opening_message": config["reply"]["opening_message"],
        "opening_messages": config["reply"]["opening_messages"],
        "opening_items": config["reply"]["opening_items"],
        "opening_interval_seconds": config["reply"]["opening_interval_seconds"],
        "tone_guidance": config["reply"]["tone_guidance"],
        "custom_guidance": config["reply"]["custom_guidance"],
        "profile_fields": config["profile_fields"],
        "lead_capture": config["lead_capture"],
        "business_rules": config["business_rules"],
        "stage_journey": config["stage_journey"],
    }
    return policy


def silence_intervals(db: Session) -> list[int]:
    return get_reception_configuration(db)["silence"]["intervals_minutes"]


def v2_silence_intervals(db: Session | None = None) -> list[int]:
    """Reviewed first silence delay; return a fresh list for each enrollment."""
    return list(get_reception_configuration(db)['silence']['v2_intervals_minutes']) if db is not None else [1, 120]


def configured_silence_nodes(
    source_nodes: list[dict],
    intervals: list[int] | None,
    *,
    silence_enabled: bool = True,
) -> list[dict]:
    """Compile initial delivery and silence nodes without coupling their switches."""
    initial_nodes = [
        deepcopy(node) for node in source_nodes if node.get("initial_delivery") is True
    ]
    if not silence_enabled:
        return initial_nodes
    if not intervals:
        return deepcopy(source_nodes)
    if not source_nodes:
        return []
    silence_templates = [
        node for node in source_nodes if node.get("initial_delivery") is not True
    ]
    if not silence_templates:
        return initial_nodes
    result: list[dict] = list(initial_nodes)
    for index, delay in enumerate(intervals):
        template = (
            silence_templates[index]
            if index < len(silence_templates)
            else silence_templates[-1]
        )
        node = deepcopy(template)
        node.update({
            "key": "silence_mainline" if index == 0 else f"wakeup_{index}",
            "journey_trigger": "silence_mainline" if index == 0 else "wakeup",
            "schedule_type": "relative",
            "basis": "enrollment" if index == 0 else "previous_node",
            "delay_minutes": int(delay),
        })
        result.append(node)
    return result


def configured_silence_ttl_hours(base_ttl_hours: float, intervals: list[int] | None) -> float:
    """Keep a journey alive long enough for all relative gaps plus a delivery buffer."""
    if not intervals:
        return float(base_ttl_hours)
    return max(float(base_ttl_hours), sum(intervals) / 60 + 24)
