from copy import deepcopy
import json

import httpx
import pytest

from app.reception_v3 import runtime
from app.reception_v3.skills import SkillRegistry


def context(route=''):
    routes = {}
    for days in (9, 11):
        routes[f'peach_{days}d_2027'] = {
            'name': f'桃花{days}日', 'aliases': [], 'version': 'test-1', 'interval_seconds': 2,
            'scripts': [{'id': f'price-{days}', 'name': '价格', 'answer_text': f'PRICE_ONLY_{days}',
                         'positive_examples': ['多少钱'], 'asset_ids': []}],
            'groups': {'map': {'text': f'INTRO_ONLY_{days}', 'assets': [f'map-{days}']}},
            'introduction_sequence': ['map'], 'facts': [{'id': 'price', 'text': f'FACT_ONLY_{days}'}]}
    return {'event': 'customer_message', 'route_variant': route, 'messages': [],
            'skills': {'routes': routes, 'reply': {}, 'silence': {}, 'lead_capture': {}, 'common_scripts': []},
            'website_facts': [{'text': 'WEB_ONLY'}], 'website_version': 'site-2',
            'available_materials': [{'key': f'map-{d}', 'routes': [f'peach_{d}d_2027']} for d in (9, 11)]}


def model(monkeypatch, responses):
    requests = []
    def post(payload, timeout):
        requests.append(deepcopy(payload))
        return httpx.Response(200, request=httpx.Request('POST', 'https://test.invalid'), json={
            'choices': [{'message': responses.pop(0)}], 'usage': {'prompt_tokens': 10, 'completion_tokens': 5}})
    monkeypatch.setattr(runtime, '_post_with_deadline', post)
    return requests


def final(**kwargs):
    return {'content': json.dumps(runtime.Decision(**kwargs).model_dump(), ensure_ascii=False)}


def tool(name, arguments, id='t1'):
    return {'id': id, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(arguments)}}


def test_selected_route_preloads_only_its_complete_config(monkeypatch):
    requests = model(monkeypatch, [final(messages=[{'text': '回答原稿'}])])
    decision, logs, _ = runtime.run_agent(context('peach_9d_2027'))
    request = json.dumps(requests[0], ensure_ascii=False)
    for marker in ('PRICE_ONLY_9', 'INTRO_ONLY_9', 'FACT_ONLY_9'):
        assert marker in request
    for marker in ('PRICE_ONLY_11', 'INTRO_ONLY_11', 'FACT_ONLY_11', 'WEB_ONLY'):
        assert marker not in request
    assert set(logs[-1]['loaded_skills']) == {'tibet-reception', 'peach-9d-2027'}
    assert decision['messages'][0]['text'] == '回答原稿'


def test_unselected_then_model_loads_both_for_comparison(monkeypatch):
    requests = model(monkeypatch, [
        {'content': None, 'tool_calls': [tool('load_skill', {'name': 'peach-9d-2027'}),
                                       tool('load_skill', {'name': 'peach-11d-2027'}, 't2')]},
        final(messages=[{'text': '两条线路比较'}])])
    _, logs, _ = runtime.run_agent(context())
    first = json.dumps(requests[0], ensure_ascii=False)
    assert 'PRICE_ONLY_9' not in first and 'PRICE_ONLY_11' not in first
    results = [m for m in requests[1]['messages'] if m['role'] == 'tool']
    assert [r['tool_call_id'] for r in results] == ['t1', 't2']
    assert 'PRICE_ONLY_9' in results[0]['content'] and 'PRICE_ONLY_11' in results[1]['content']
    assert len(logs[-1]['loaded_skills']) == 3


def test_website_is_only_loaded_when_requested(monkeypatch):
    requests = model(monkeypatch, [{'content': '', 'tool_calls': [tool('get_service_facts', {})]}, final()])
    runtime.run_agent(context('peach_11d_2027'))
    assert 'WEB_ONLY' not in json.dumps(requests[0])
    assert 'WEB_ONLY' in json.dumps(requests[1])


def test_live_configuration_changes_loaded_text_and_digest():
    c = context()
    before = SkillRegistry(c['skills']).load('peach-9d-2027')
    c['skills']['routes']['peach_9d_2027']['scripts'][0]['answer_text'] = '后台刚保存的新原话'
    after = SkillRegistry(c['skills']).load('peach-9d-2027')
    assert '后台刚保存的新原话' in after['instructions']
    assert 'PRICE_ONLY_9' not in after['instructions']
    assert before['digest'] != after['digest']


def test_common_scenario_and_only_v3_reasoning_settings_reach_model(monkeypatch):
    c = context()
    c['skills']['common_scripts'] = [{'id': 'common', 'name': '示例',
        'scenario': '客户询问证件交付方式', 'text': '当前通用原话', 'enabled': True}]
    c['skills']['reply'] = {'tone_guidance': '当前语气', 'opening_message': '开场由发送层执行'}
    c['skills']['lead_capture'] = {'enabled': False, 'channels': ['微信'], 'ask_after_answered_topics': 2}
    requests = model(monkeypatch, [final()])
    runtime.run_agent(c)
    prompt = requests[0]['messages'][0]['content']
    assert '客户询问证件交付方式' in prompt and '当前通用原话' in prompt
    assert '当前语气' in prompt and '"enabled": false' in prompt
    assert 'ask_after_answered_topics' not in prompt
    assert '开场由发送层执行' not in prompt
    assert prompt.count('## 执行协议') == 1


def test_route_applicability_survives_compilation_and_loading(monkeypatch):
    from app.reception_v3 import skills
    source = {'name': '线路', 'package_version': 'v1', 'knowledge_facts': [],
              'initial_delivery_interval_seconds': 2, 'introduction_sequence': [], 'groups': {},
              'ai_guidance': '这条线路专属接待说明',
              'fixed_answers': [{'id': 'group', 'name': '人数', 'status': 'active',
                'answer_text': '原话', 'positive_examples': ['小团'], 'negative_examples': ['包团'],
                'party_size_min': 4, 'party_size_max': 6, 'usage_note': '报价不等于已成团',
                'topics': ['price']} ]}
    monkeypatch.setattr(skills, 'ROUTES', {'peach_9d_2027': source})
    monkeypatch.setattr(skills, 'ensure_route_packages_current', lambda: None)
    monkeypatch.setattr(skills, 'get_reception_configuration', lambda db: context()['skills'])
    body = skills.SkillRegistry(skills.compile_skills(None)).load('peach-9d-2027')['instructions']
    for marker in ('这条线路专属接待说明', '包团', '"party_size_min": 4',
                   '"party_size_max": 6', '报价不等于已成团'):
        assert marker in body


def test_standard_metadata_and_bad_skill_error(tmp_path):
    registry = SkillRegistry(context()['skills'])
    assert len(registry.index()) == 3
    folder = tmp_path / 'broken'
    folder.mkdir()
    (folder / 'SKILL.md').write_text('# no metadata', encoding='utf-8')
    with pytest.raises(ValueError, match='frontmatter_missing'):
        SkillRegistry(context()['skills'], root=tmp_path)


def test_unknown_skill_is_a_tool_error_then_model_can_correct(monkeypatch):
    requests = model(monkeypatch, [
        {'content': None, 'tool_calls': [tool('load_skill', {'name': 'missing'})]},
        {'content': None, 'tool_calls': [tool('load_skill', {'name': 'peach-11d-2027'}, 't2')]},
        final(route_variant='peach_11d_2027', start_introduction=True)])
    decision, logs, _ = runtime.run_agent(context())
    assert 'skill_not_found' in requests[1]['messages'][-1]['content']
    assert decision['start_introduction']
    assert set(logs[-1]['loaded_skills']) == {'tibet-reception', 'peach-11d-2027'}


def test_switch_loads_new_route_without_losing_conversation(monkeypatch):
    c = context('peach_9d_2027')
    c['messages'] = [{'direction': 'incoming', 'content': '改成11日，我们两位'}]
    requests = model(monkeypatch, [
        {'content': None, 'tool_calls': [tool('load_skill', {'name': 'peach-11d-2027'})]},
        final(route_variant='peach_11d_2027', interrupt=True, start_introduction=True, profile={'party_size': 2})])
    decision, _, _ = runtime.run_agent(c)
    assert decision['profile']['party_size'] == 2
    assert '改成11日' in requests[1]['messages'][1]['content']


def test_timer_uses_same_selected_skill_and_preserves_output(monkeypatch):
    c = context('peach_9d_2027')
    c.update(event='silence_due', profile={'party_size': 2})
    requests = model(monkeypatch, [final(messages=[{'text': '您們日期商量好了嗎？', 'script_id': 'date'}])])
    decision, logs, _ = runtime.run_agent(c)
    assert 'silence_due' in requests[0]['messages'][1]['content']
    assert decision['messages'][0]['text'] == '您們日期商量好了嗎？'
    assert logs[-1]['script_ids'] == ['date']


def test_model_selected_route_activates_body_before_final_decision(monkeypatch):
    requests = model(monkeypatch, [final(route_variant='peach_11d_2027', start_introduction=True),
                                   final(route_variant='peach_11d_2027', start_introduction=True)])
    _, logs, _ = runtime.run_agent(context())
    assert 'INTRO_ONLY_11' not in json.dumps(requests[0])
    assert 'INTRO_ONLY_11' in json.dumps(requests[1])
    assert logs[0]['status'] == 'skill_activation'
    assert 'peach-11d-2027' in logs[-1]['loaded_skills']
