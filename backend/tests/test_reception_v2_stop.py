from app.deepseek_evaluation import EvaluationDecision


def test_explicit_stop_is_normalized_to_durable_stop_and_acknowledgement():
    from app.decision_service import generate_decision

    def model_call(_packet):
        return EvaluationDecision(
            action="no_action", branch="unclassified", intent="other",
            reply=None, safety_flags=["opt_out_request"], confidence=.9,
            v2_events=[{'type': 'contact_refused', 'scope': 'all', 'quote': '请不要再联系我'}],
        ), [{"attempt": 1}], "digest"

    decision, _logs, _digest, _trace = generate_decision({
        "module": "reply", "engine_version": "v2",
        "customer_text": "\u4e0d\u7528\u4e86\uff0c\u8bf7\u4e0d\u8981\u518d\u8054\u7cfb\u6211",
        "context_messages": [],
    }, model_call=model_call)

    assert decision.action == "reply"
    assert "stop_automation" in decision.safety_flags
    assert decision.wakeup_action == "skip"
    assert decision.reply
