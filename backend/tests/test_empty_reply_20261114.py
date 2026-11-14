import pytest

from app.model_transport import EvaluationCallError
from app.reception_v3 import runtime
from app.reception_v3.action_output import validate_reply_output
from test_reception_v3_skills import context, model, final


@pytest.mark.parametrize('messages', [[], [{}], [{'text': ' \n'}]])
def test_empty_reply_is_not_success(messages):
    with pytest.raises(ValueError, match='empty_reply_action'):
        validate_reply_output({'action': 'reply', 'messages': messages}, {})


@pytest.mark.parametrize('decision,state', [
    ({'action': 'wait'}, {}), ({'action': 'handoff'}, {}), ({'action': 'queue'}, {}),
    ({'action': 'reply', 'start_introduction': True}, {}),
    *[({'action': 'reply', 'messages': [m]}, {}) for m in [
        {'text': '您好'}, {'script_id': 'price'}, {'asset_keys': ['car']},
        {'quote_options': ['price']}, {'material_rule_id': 'oxygen'}]],
    ({'action': 'reply'}, {'delivery_kind': 'introduction'}),
    ({'action': 'reply'}, {'intake_status': 'waiting'}),
])
def test_real_actions_and_existing_plans_are_preserved(decision, state):
    validate_reply_output(decision, {'state': state})


def test_1015_empty_reply_is_corrected_within_same_call(monkeypatch):
    c = context('peach_11d_2027')
    c['state'] = {'opening_started': False}
    c['messages'] = [{'direction': 'incoming', 'content': '3~4月【西藏林芝桃花節 11日】上珠峰'}]
    requests = model(monkeypatch, [final(reason='交由系統人數問題後再介紹'),
                                  final(start_introduction=True, route_variant='peach_11d_2027')])
    result, logs, _ = runtime.run_agent(c)
    assert result['start_introduction']
    assert len(requests) == 2
    assert 'empty_reply_action' in requests[1]['messages'][-1]['content']
    assert logs[0]['status'] == 'invalid_json'


def test_repeated_empty_reply_fails_instead_of_completing(monkeypatch):
    requests = model(monkeypatch, [final(), final()])
    with pytest.raises(EvaluationCallError, match='empty_reply_action'):
        runtime.run_agent(context('peach_11d_2027'))
    assert len(requests) == 2
