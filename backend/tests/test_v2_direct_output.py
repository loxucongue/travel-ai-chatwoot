"""V2 final copy is the model's output, not a second model's rewrite."""
import json

import pytest

from app.decision_service import generate_decision
from app.reception_v2 import runtime


@pytest.mark.parametrize('body', [
    '我幫您比較兩條線路，也可以一起看酒店安排。',
    '您在意住宿嗎？還是更想先看景點？',
    '住宿和交通安排可以一起看。' * 25,
    '简体原文和帳號 abc_123 保持原样：https://example.test/酒店?q=比较',
])
def test_final_copy_survives_runtime_and_decision_service_without_audit(monkeypatch, body):
    calls = []
    def unexpected(*args, **kwargs):
        pytest.fail('V2 must not call a legacy verifier or copy rewriter')
    monkeypatch.setattr('app.reply_fact_verification.call_reply_fact_verifier', unexpected)
    monkeypatch.setattr('app.reply_generation._normalize_taiwan_service_terms', unexpected)
    monkeypatch.setattr(runtime.settings, 'deepseek_api_key', 'test')
    def call(payload, index):
        calls.append(payload)
        return {'content': json.dumps({
            'action': 'reply', 'route_variant': 'peach_9d_2027', 'intent': 'other',
            'reply': body, 'v2_events': [], 'evidence_refs': [],
        }, ensure_ascii=False)}, {'round': index, 'duration_ms': 1}
    monkeypatch.setattr(runtime, '_call', call)
    decision, logs, _, trace = generate_decision({
        'engine_version': 'v2', 'module': 'reply', 'customer_text': '住宿呢？',
        'route_variant': 'peach_9d_2027',
        'context_messages': [{'direction': 'incoming', 'content': '想了解九日行程'}],
    })
    assert decision.reply == body
    assert len(calls) == 1
    assert not any('verif' in row.get('node', '') or 'revision' in row.get('node', '') for row in logs)
    assert trace['output_mode'] == 'direct_model_output'


def test_route_skill_reads_current_enabled_scripts_with_assets(monkeypatch):
    from app.reception_v2.skill_registry import SkillRegistry
    from app.route_packages import ROUTES
    from copy import deepcopy
    route = deepcopy(ROUTES['peach_9d_2027'])
    script = {'id': 'hotel_script', 'status': 'active', 'answer_text': '這是飯店介紹原文。',
              'fact_ids': ['route.shared.hotel_reference'], 'asset_ids': ['hotel-photo'],
              'positive_examples': ['住宿如何'], 'negative_examples': []}
    route['fixed_answers'] = [script, {**script, 'id': 'draft', 'status': 'pending_review'}]
    monkeypatch.setitem(ROUTES, 'peach_9d_2027', route)
    registry = SkillRegistry()
    loaded = registry.load('peach-9d-2027')
    assert loaded['scripts'] == [script]
    assert loaded['introduction']
    loaded['scripts'][0]['answer_text'] = '不得修改源数据'
    assert route['fixed_answers'][0]['answer_text'] == '這是飯店介紹原文。'
    route['fixed_answers'][0]['answer_text'] = '更新后的原文。'
    assert registry.load('peach-9d-2027')['scripts'][0]['answer_text'] == '更新后的原文。'


def test_repeated_identical_customer_message_is_not_another_opening():
    context = {'module': 'reply', 'customer_text': '你好',
               'context_messages': [{'direction': 'incoming', 'content': '你好'}]}
    assert not runtime._is_first_customer_message(context)
    from app.deepseek_evaluation import EvaluationDecision
    decision = EvaluationDecision('reply', 'unclassified', 'other', reply='原文')
    runtime._attach_configured_opening(context, decision)
    assert decision.opening_items == []


@pytest.mark.parametrize('text', ['你好', '我想了解11日行程', '住宿和費用怎麼安排？', '我先考慮一下'])
def test_first_contact_uses_configured_opening_regardless_of_text(monkeypatch, text):
    monkeypatch.setattr(runtime.settings, 'deepseek_api_key', 'test')
    monkeypatch.setattr(runtime, '_call', lambda *args: ({'content': json.dumps({
        'action': 'reply', 'intent': 'other', 'reply': '自然承接', 'v2_events': [],
    })}, {'round': 0, 'duration_ms': 1}))
    decision, logs, _, trace = runtime.run_v2_agent({
        'module': 'reply', 'customer_text': text, 'context_messages': [],
        'reception_policy': {'operator_configuration': {
            'opening_messages': ['第一段原文。', '第二段原文。'], 'opening_interval_seconds': 3,
        }},
    })
    assert decision.opening_messages == ['第一段原文。', '第二段原文。']
    assert decision.opening_interval_seconds == 3
    assert len(logs) == 1


def test_model_cannot_reopen_a_conversation_by_setting_opening_intent():
    context = {'module': 'reply', 'customer_text': '你好',
               'context_messages': [{'direction': 'incoming', 'content': '你好'}]}
    decision = runtime._validated_decision({'content': json.dumps({
        'action': 'reply', 'intent': 'other', 'reply': '還想了解哪些內容呢？',
        'delivery_intent': 'opening', 'v2_events': [],
    })}, set(), set(), context)
    runtime._enforce_delivery_contract(context, decision)
    assert decision.reply == '還想了解哪些內容呢？'
    assert decision.opening_items == [] and decision.opening_messages == []


@pytest.mark.parametrize('delivery_intent,continuation', [('opening', False), ('none', True)])
def test_first_contact_opening_does_not_duplicate_generic_route_question(delivery_intent, continuation):
    from app.deepseek_evaluation import EvaluationDecision
    decision = EvaluationDecision('reply', 'unclassified', 'other', reply='模型答案',
        v2_events=[{'type': 'question', 'quote': '想了解西藏'}])
    decision.delivery_intent = delivery_intent
    runtime._attach_configured_opening({'module': 'reply', 'context_messages': [],
        'reception_policy': {'operator_configuration': {'opening_messages': ['配置欢迎', '配置选线']}}}, decision)
    assert decision.opening_messages == ['配置欢迎', '配置选线']
    assert decision.opening_continuation is continuation
    assert decision.reply == ('模型答案' if continuation else '配置欢迎')
