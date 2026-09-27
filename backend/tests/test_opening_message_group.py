from copy import deepcopy

import pytest

from app.reception_config import ReplySettings
from app.reply_planning import build_reply_plan
from app.reply_generation import call_reply_generator
from app.reply_understanding import CustomerUnderstanding
from test_advisor_feedback_revision import packet


def test_opening_group_round_trip(authenticated):
    client, csrf = authenticated
    config = client.get('/v1/automation/reception-config').json()['config']
    config['reply']['opening_messages'] = ['您好～', '請問想了解哪條行程呢？']
    config['reply']['opening_interval_seconds'] = 3
    result = client.patch('/v1/automation/reception-config', json={'reply': {
        'opening_items': [{'key':f'text-{index}','content_type':'text','content':text}
                          for index,text in enumerate(config['reply']['opening_messages'])],
        'opening_interval_seconds':3}}, headers={'X-CSRF-Token': csrf})
    assert result.status_code == 200, result.text
    saved = client.get('/v1/automation/reception-config').json()['config']['reply']
    assert saved['opening_messages'] == config['reply']['opening_messages']
    assert saved['opening_interval_seconds'] == 3
    assert saved['opening_message'] == '您好～'


@pytest.mark.parametrize('messages', [[], [' '], ['x'] * 11, ['x' * 201]])
def test_invalid_opening_groups_are_rejected(messages):
    with pytest.raises(ValueError):
        ReplySettings(opening_messages=messages)


def test_group_uses_per_message_budget_and_no_model_rewriting():
    context = packet()
    messages = ['您好。' * 30, '請看。' * 30, '選擇哪條呢？']
    context['reception_policy']['operator_configuration'].update(
        opening_messages=messages, opening_interval_seconds=3,
    )
    plan = build_reply_plan(context, CustomerUnderstanding(intent='other', semantic_signals=['general_inquiry']))
    reply, logs, _ = call_reply_generator(context, plan)
    assert reply.body == '\n\n'.join(messages)
    assert plan.opening_messages == messages and plan.opening_interval_seconds == 3
    assert reply.asset_ids == [] and logs == []
