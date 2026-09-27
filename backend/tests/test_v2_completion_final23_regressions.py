import pytest
import json


def test_v2_parser_does_not_inject_legacy_route_citations():
    from app.deepseek_evaluation import EvaluationDecision
    from app.reception_v2.runtime import _validated_decision
    raw={'action':'reply','intent':'other','branch':'unclassified','reply':'我們有桃花9日和桃花加珠峰11日行程。',
         'v2_events':[],'evidence_refs':[],'reply_options':['桃花9日','桃花+珠峰11日']}
    assert EvaluationDecision.parse(raw).evidence_refs
    decision=_validated_decision({'content':json.dumps(raw)},set(),set(),{'customer_text':'你好'})
    assert decision.evidence_refs==[]
    replay={**raw,'reply':decision.reply,'evidence_refs':decision.evidence_refs}
    assert _validated_decision({'content':json.dumps(replay)},set(),set(),{'customer_text':'你好'}).reply
    with pytest.raises(ValueError,match='unknown_evidence_reference'):
        _validated_decision({'content':json.dumps({**raw,'evidence_refs':['route.9.overview']})},set(),set(),{})
