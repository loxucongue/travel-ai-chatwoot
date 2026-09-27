import pytest
import json


@pytest.mark.parametrize('reply,accepted',[
    ('機票不在包含範圍內，需要另外處理。', True),
    ('機票不在包含項目裡。', True),
    ('機票不包含在9980裡。', True),
    ('機票包含在9980裡。', False),
    ('機票是否包含需要確認。', False),
])
def test_flight_exclusion_acceptance_supports_equivalent_wording(reply, accepted):
    import re
    from test_v2_service_acceptance import CASES
    pattern = next(case[4][1] for case in CASES if case[0] == 'flight')
    assert bool(re.search(pattern, reply)) is accepted


def test_customer_turn_requires_a_reply_action():
    from app.reception_v2.runtime import _validated_decision
    raw={'action':'no_action','intent':'other','reply':'好的，不再打擾。','v2_events':[]}
    with pytest.raises(ValueError,match='v2_customer_reply_required'):
        _validated_decision({'content':json.dumps(raw)},set(),set(),{'customer_text':'不要再推銷'})
    d=_validated_decision({'content':json.dumps(raw)},set(),set(),{'module':'silence_touch','customer_text':'不要再推銷'})
    assert d.action=='no_action'
