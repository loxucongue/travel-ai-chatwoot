from dataclasses import replace

import pytest

from app.reception_config import BusinessRule
from app.reply_generation import deterministic_system_reply
from app.reply_planning import build_reply_plan
from app.reply_understanding import CustomerUnderstanding
from test_split_realtime_reply import context


@pytest.mark.parametrize("rule_id", ["operator_one", "renamed_rule"])
@pytest.mark.parametrize("matched", [False, True])
def test_bound_outside_rule_does_not_require_model_rule_id(rule_id, matched):
    ctx = context(customer_text="我想要北京行程")
    ctx["reception_policy"]["operator_configuration"]["business_rules"] = [
        BusinessRule(id=rule_id, name="Outside", condition="Outside catalog",
                     action="handoff", trigger="outside_catalog").model_dump()
    ]
    u = CustomerUnderstanding(intent="other", route_resolution="outside_catalog")
    if matched:
        u = replace(u, matched_rule_ids=[rule_id], matched_rule_evidence={rule_id: "北京行程"})
    assert build_reply_plan(ctx, u).action == "handoff"
    ctx["reception_policy"]["operator_configuration"]["business_rules"][0]["enabled"] = False
    assert build_reply_plan(ctx, u).action == "reply"


def test_bound_rule_cannot_fall_back_to_model_match():
    ctx = context()
    ctx["reception_policy"]["operator_configuration"]["business_rules"] = [
        dict(id="outside", enabled=True, action="handoff", trigger="outside_catalog")
    ]
    u = CustomerUnderstanding(intent="other", route_resolution="none",
                              matched_rule_ids=["outside"], matched_rule_evidence={"outside": "過去去過北京"})
    assert build_reply_plan(ctx, u).action == "reply"


def test_customization_copy_does_not_invent_preferences_or_completed_handoff():
    u = CustomerUnderstanding(intent="other", semantic_signals=["customization_request"])
    plan = build_reply_plan(context(customer_text="可以私人包團嗎", route_variant="peach_11d_2027"), u)
    reply = deterministic_system_reply(plan).reply
    assert plan.action == "reply"
    assert "另外規劃" in reply
    assert all(term not in reply for term in ("想自助", "控制預算", "交給顧問", "已安排"))


def test_unknown_trigger_is_rejected():
    with pytest.raises(ValueError):
        BusinessRule(id="test", name="Test", condition="Test", action="handoff", trigger="guess")
