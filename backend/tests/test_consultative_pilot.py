import json
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest

from app.config import settings
from app.reception_v3 import consultative, runtime, route_understanding
from app.reception_v3.skills import SkillRegistry
from test_reception_v3_skills import context as skill_context


@pytest.fixture
def pilot(monkeypatch):
    monkeypatch.setattr(settings, 'reception_consultative_pilot_enabled', True)
    monkeypatch.setattr(settings, 'app_profile', 'evaluation')
    monkeypatch.setattr(settings, 'outbound_mode', 'disabled')
    monkeypatch.setattr(settings, 'chatwoot_write_enabled', False)
    monkeypatch.setenv('BUSINESS_PROFILE', 'china2go')


@pytest.mark.parametrize('setting,value', [('app_profile', 'production'),
    ('chatwoot_write_enabled', True), ('reception_consultative_pilot_enabled', False)])
def test_pilot_cannot_activate_in_production_or_writable_environment(pilot, monkeypatch, setting, value):
    assert consultative.enabled()
    monkeypatch.setattr(settings, setting, value)
    assert not consultative.enabled()


def test_other_business_does_not_inherit_pilot(pilot, monkeypatch):
    monkeypatch.setenv('BUSINESS_PROFILE', 'rujing2')
    assert not consultative.enabled()


def context():
    return {**skill_context('peach_9d_2027'), 'event': 'customer_message', 'route_variant': 'peach_9d_2027',
            'available_materials': [], 'profile': {}, 'state': {}, 'now': '2026-11-01T04:00:00Z',
            'messages': [{'id': 'c1', 'direction': 'incoming', 'content': '這條有珠峰嗎？', 'status': 'received'}]}


def raw():
    return {'action': 'reply', 'messages': [{'text': '桃花9日不安排珠峰。'}]}


def test_one_call_understands_and_answers_with_execution_provenance(pilot, monkeypatch):
    monkeypatch.setattr(settings, 'reception_route_understanding_enabled', True)
    monkeypatch.setattr(runtime, 'interpret', lambda *a: pytest.fail('must not call separate semantic model'))
    sent = []
    def post(payload, timeout):
        sent.append(payload)
        return httpx.Response(200, request=httpx.Request('POST', 'https://test.invalid'),
                              json={'choices': [{'message': {'content': json.dumps(raw())}}]})
    monkeypatch.setattr(runtime, '_post_with_deadline', post)
    c = context()
    original = deepcopy(c)
    decision, logs, digest = runtime.run_agent(c)
    assert len(sent) == 1 and decision['messages'][0]['text'] == '桃花9日不安排珠峰。'
    assert decision['profile'] == {} and c == original and digest
    assert route_understanding.from_run(SimpleNamespace(trace={'logs': logs}, input_snapshot=c))['intent'] == 'other'
    assert 'delivery_review' not in str(sent) and 'generation_instruction' not in str(sent)


def test_unrelated_fact_question_does_not_require_preference_update():
    decision, interpretation, log = consultative.unpack(raw(), context())
    assert 'profile' not in decision and interpretation['intent'] == 'other'
    assert log['owner'] == consultative.VERSION


@pytest.mark.parametrize('evidence', [{}, {'everest_preference': {'message_id': 'old', 'quote': '不要珠峰'}},
    {'everest_preference': {'message_id': 'c1', 'quote': '不要珠峰'}}])
def test_preference_changes_require_current_real_customer_evidence(evidence):
    data = {**raw(), 'profile': {'everest_preference': 'avoid'}, 'memory_evidence': evidence}
    with pytest.raises(ValueError):
        consultative.unpack(data, context())


def test_timer_cannot_manufacture_customer_preferences():
    c = {**context(), 'event': 'silence_due'}
    data = {'action': 'wait', 'profile': {'everest_preference': 'avoid'},
            'memory_evidence': {'everest_preference': {'message_id': 'c1', 'quote': '這條有珠峰嗎？'}}}
    with pytest.raises(ValueError, match='requires_customer_source'):
        consultative.unpack(data, c)


def test_image_only_reply_needs_no_fabricated_text_evidence():
    c = context()
    c['messages'][0]['content'] = ''
    c['messages'][0]['content_type'] = 'image'
    _, interpreted, log = consultative.unpack({'action': 'wait'}, c)
    assert interpreted is None and log is None


def test_actual_selection_requires_source_but_not_fixed_action_words():
    c = context()
    c['messages'][0]['content'] = '九日的那個'
    data = {'route_choice': 'selected', 'route_variant': 'peach_9d_2027',
            'selection_evidence': {'message_id': 'c1', 'quote': '九日的那個'}}
    _, interpreted, _ = consultative.unpack(data, c)
    assert interpreted['selected_route'] == 'peach_9d_2027'
    data.pop('selection_evidence')
    with pytest.raises(ValueError):
        consultative.unpack(data, c)


def test_repair_keeps_pilot_wire_fields():
    original = runtime.Decision.model_json_schema()
    fixed = consultative.repair_schema(original)
    assert {'selection_evidence', 'memory_evidence'} <= fixed['properties'].keys()
    assert 'selection_evidence' not in original['properties']


def test_initial_task_uses_current_product_and_preserves_customer_context():
    c = context()
    current = {'route_facts': {'unrelated': 'not yet read'},
               'pricing_evidence': {'peach_9d_2027': {'price': 100}, 'other': {'price': 200}},
               'profile': deepcopy(c['profile'])}
    consultative.attach_task(current, c)
    assert 'route_facts' not in current
    assert 'pricing_evidence' not in current
    assert current['profile'] == c['profile']
    assert current['dialogue_evidence']['history'][0]['text'] == c['messages'][0]['content']


def test_contact_permission_change_requires_customer_evidence():
    with pytest.raises(ValueError, match='requires_customer_source:opt_out'):
        consultative.unpack({'action': 'wait', 'opt_out': True}, context())


def test_prices_are_read_separately_without_recalculating_or_rewriting():
    c = context()
    route = c['skills']['routes']['peach_9d_2027']
    script = {'id': 'price_shared_2', 'answer_text': 'configured quote', 'party_size_min': 2}
    route['scripts'].append(script)
    projected = json.loads(consultative.product_document(SkillRegistry(c['skills']), 'peach-9d-2027')['instructions'])
    assert script not in projected['scripts']
    assert script in consultative.price_templates(c['skills'], 'peach_9d_2027')['scripts']


def test_data_projection_removes_sop_without_mutating_facts_or_prices():
    b = skill_context('peach_9d_2027')['skills']
    original = deepcopy(b)
    registry = SkillRegistry(b)
    projected = consultative.product_document(registry, 'peach-9d-2027')
    data = json.loads(projected['instructions'])
    assert data['facts'] == b['routes']['peach_9d_2027']['facts']
    assert [(s['id'], s['answer_text']) for s in data['scripts']] == [
        (s['id'], s['answer_text']) for s in b['routes']['peach_9d_2027']['scripts'] if not s['id'].startswith('price')]
    assert b == original and 'groups' not in data and 'guidance' not in data
