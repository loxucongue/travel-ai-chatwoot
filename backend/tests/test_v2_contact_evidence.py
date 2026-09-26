import json
import pytest
from app.reception_v2.runtime import _validated_decision


@pytest.mark.parametrize('text,channel,value,valid',[
    ('微信是test_traveller26，請顧問聯絡','wechat','test_traveller26',True),
    ('Email是Traveller@example.com','email','Traveller@example.com',True),
    ('請問在哪集合？','wechat','invented_id',False),
    ('之前給過微信了','wechat','test_traveller26',False),
    ('我加你LINE了','line','LINE',False),
])
def test_capture_requires_current_literal_valid_identifier(text,channel,value,valid):
    raw={'action':'handoff','route_variant':'peach_9d_2027','reply':'聯絡方式已收到。',
        'lead_action':'captured','contact_values':{channel:value},'handoff_reason':'lead_captured',
        'v2_events':[{'type':'human_requested','quote':text}]}
    context={'module':'reply','customer_text':text,
        'context_messages':[{'role':'customer','content':'微信是test_traveller26'}]}
    if valid:
        decision=_validated_decision({'content':json.dumps(raw)},set(),set(),context)
        assert decision.contact_values=={channel:value}
    else:
        with pytest.raises(ValueError,match='v2_contact_value_without_current_evidence'):
            _validated_decision({'content':json.dumps(raw)},set(),set(),context)
