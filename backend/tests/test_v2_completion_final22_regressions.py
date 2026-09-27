import json
import pytest
from app.reception_v2 import runtime

@pytest.fixture(autouse=True)
def configured_opening(monkeypatch):
    from app.reception_config import default_reception_configuration,policy_from_configuration
    from app.reception_policy_views import views_for_context
    policy=policy_from_configuration(default_reception_configuration())
    monkeypatch.setattr(runtime,'views_for_context',lambda context:views_for_context({'reception_policy':policy,**context}))


def test_reply_body_alias_is_the_same_text_not_a_generated_fallback():
    raw={'action':'reply','reply_body':'在林芝集合。','v2_events':[{'type':'question','quote':'在哪集合？'}]}
    result=runtime._validated_decision({'content':json.dumps(raw)},set(),set(),{'module':'reply','customer_text':'在哪集合？'})
    assert result.reply=='在林芝集合。'

@pytest.mark.parametrize('context,events',[
    ({'route_variant':'peach_9d_2027'},[]),
    ({'context_messages':[{'role':'assistant','content':'之前的回答'}]},[]),
    ({},[{'type':'question','quote':'在哪集合？'}]),
])
def test_empty_opening_cannot_bypass_bound_route_history_or_question(context,events):
    raw={'action':'reply','delivery_intent':'opening','v2_events':events}
    with pytest.raises(ValueError,match='reply_missing'):
        runtime._validated_decision({'content':json.dumps(raw)},set(),set(),{'module':'reply','customer_text':'在哪集合？',**context})
