"""Learning never changes route ownership, facts or permission boundaries."""
from copy import deepcopy
from datetime import datetime
import json

import pytest

from app.config import settings
from app.reception_v3 import advisor_experience as experience
from app.reception_v3.skills import SkillRegistry


@pytest.fixture
def library(monkeypatch):
    class EvaluationClock:
        @staticmethod
        def now(tz):
            return datetime(2026, 11, 1, 12, tzinfo=tz)
    # The pilot expiry is operational data, not the date when tests happen to run.
    monkeypatch.setattr(experience, 'datetime', EvaluationClock)
    monkeypatch.setattr(settings, 'reception_advisor_experience_enabled', True)
    monkeypatch.setenv('BUSINESS_PROFILE', 'china2go')
    return experience.Library.model_validate_json(experience.LIBRARY_PATH.read_text(encoding='utf-8'))


def catalog(library):
    routes = {}
    for entry in library.experiences:
        for route, refs in entry.route_fact_ids.items():
            record = routes.setdefault(route, {'facts': []})
            record['facts'].extend({'id': ref, 'text': 'current approved fact'} for ref in refs)
    return {'routes': routes}


@pytest.mark.parametrize('route', [
    'peach_9d_2027', 'peach_11d_2027', 'tibet_essence_8d', 'tibet_overview_10d',
    'tibet_linzhi_8d', 'tibet_winter_8d'])
def test_each_route_only_receives_own_fact_references(library, route):
    bundle = catalog(library)
    before = deepcopy(bundle)
    text, ids = experience.render(library, bundle, route)
    rows = json.loads(text[text.index('[{'):])
    allowed = {f['id'] for f in bundle['routes'][route]['facts']}
    assert len(ids) == 3 and all(set(r['本线路事实ID']) <= allowed for r in rows)
    assert 'advisor.meals' not in ids  # Held back after real-model overstatement.
    assert bundle == before
    # References belonging to an unrelated eight-day product must never leak.
    for other in ('tibet_essence_8d', 'tibet_linzhi_8d', 'tibet_winter_8d'):
        if other != route:
            assert other not in text


def test_unknown_product_never_inherits_another_routes_methods(library):
    assert experience.render(library, catalog(library), 'other_company_route') == ('', [])
    text, ids = experience.render(library, catalog(library))
    assert ids == ['advisor.need_clarification']
    assert not any(route in text for route in catalog(library)['routes'])


def test_missing_facts_disable_only_affected_card_and_preserve_original_data(library):
    bundle = catalog(library)
    route = 'tibet_winter_8d'
    bundle['routes'][route]['facts'] = [f for f in bundle['routes'][route]['facts'] if f['id'] != route + '.hotel']
    text, ids = experience.render(library, bundle, route)
    assert 'advisor.hotel' not in ids and len(ids) == 2
    assert 'advisor.arrival' in ids


@pytest.mark.parametrize('profile,enabled,business,available', [
    ('evaluation', True, 'china2go', True), ('production', True, 'china2go', False),
    ('evaluation', False, 'china2go', False), ('evaluation', True, 'rujing2', False)])
def test_pilot_and_switch_cannot_publish_implicitly(library, monkeypatch, profile, enabled, business, available):
    monkeypatch.setattr(settings, 'app_profile', profile)
    monkeypatch.setattr(settings, 'reception_advisor_experience_enabled', enabled)
    monkeypatch.setenv('BUSINESS_PROFILE', business)
    assert (experience.load_library() is not None) is available


@pytest.mark.parametrize('outbound_mode', ['disabled', 'live'])
def test_pilot_cannot_load_when_channel_writes_are_allowed(library, monkeypatch, outbound_mode):
    monkeypatch.setattr(settings, 'outbound_mode', outbound_mode)
    monkeypatch.setattr(settings, 'chatwoot_write_enabled', True)
    assert experience.load_library() is None


@pytest.mark.parametrize('change', ['expired', 'paused', 'malformed', 'missing'])
def test_optional_library_failure_does_not_break_reception(library, monkeypatch, tmp_path, change):
    path = tmp_path / 'library.json'
    data = library.model_dump(mode='json')
    if change == 'expired':
        data['review_due'] = '2000-01-01'
    if change == 'paused':
        data['status'] = 'paused'
    if change != 'missing':
        path.write_text('{' if change == 'malformed' else json.dumps(data), encoding='utf-8')
    monkeypatch.setattr(experience, 'LIBRARY_PATH', path)
    assert experience.load_library() is None


def test_disable_one_entry_and_reject_ambiguous_scope(library):
    library.experiences[2].enabled = False
    assert 'advisor.vehicle' not in experience.render(library, catalog(library), 'peach_9d_2027')[1]
    data = library.experiences[2].model_dump()
    data['route_fact_ids'] = {}
    with pytest.raises(ValueError, match='route_method_requires_evidence'):
        experience.Experience.model_validate(data)


def test_shared_skill_has_methods_only_and_switch_restores_exact_baseline(library, monkeypatch):
    bundle = {**catalog(library), 'common_scripts': []}
    original = deepcopy(bundle)
    enabled = SkillRegistry(bundle).load('tibet-reception')
    monkeypatch.setattr(settings, 'reception_advisor_experience_enabled', False)
    disabled = SkillRegistry(bundle).load('tibet-reception')
    assert '顾问经验参考' in enabled['instructions']
    assert 'advisor.need_clarification' in enabled['instructions']
    assert 'advisor.meals' not in enabled['instructions']
    assert '顾问经验参考' not in disabled['instructions']
    assert bundle == original and enabled['digest'] != disabled['digest']


def test_model_keeps_its_complete_answer_and_trace_does_not_claim_usage(library, monkeypatch):
    from app.reception_v3 import runtime
    from test_reception_v3_skills import context, model, final
    monkeypatch.setattr(settings, 'reception_route_understanding_enabled', False)
    c = context('peach_9d_2027')
    c['skills']['routes']['peach_9d_2027']['facts'] = catalog(library)['routes']['peach_9d_2027']['facts']
    original_text = '這條的早餐有包含；您剛剛問的正餐則是自理。'
    requests = model(monkeypatch, [final(messages=[{'text': original_text}])])
    decision, logs, _ = runtime.run_agent(c)
    assert len(requests) == 1 and decision['messages'][0]['text'] == original_text
    evidence = next(x['advisor_experience'] for x in logs if x.get('node') == 'reception_v3')
    assert evidence['status'] == 'provided_not_verified_used'
    assert evidence['provided_by_skill']['peach-9d-2027'] == [
        'advisor.vehicle', 'advisor.arrival', 'advisor.hotel']
    assert evidence['version'] == library.version


def test_comparison_loads_separate_route_evidence_without_selecting_route(library, monkeypatch):
    from app.reception_v3 import runtime
    from test_reception_v3_skills import context, model, final, tool
    monkeypatch.setattr(settings, 'reception_route_understanding_enabled', False)
    c = context('peach_9d_2027')
    for key, value in c['skills']['routes'].items():
        value['facts'] = catalog(library)['routes'][key]['facts']
    requests = model(monkeypatch, [
        {'content': '', 'tool_calls': [tool('load_skill', {'name': 'peach-11d-2027'})]},
        final(messages=[{'text': '九日不去珠峰，十一日包含珠峰，兩條住宿安排要分開看。'}])])
    decision, logs, _ = runtime.run_agent(c)
    result = next(m['content'] for m in requests[1]['messages'] if m['role'] == 'tool')
    assert 'advisor.hotel' in result and 'route.11.rongbuk' in result
    assert decision['route_choice'] == 'unchanged' and not decision['route_variant']
    assert not decision['start_introduction']
    evidence = next(x['advisor_experience'] for x in reversed(logs) if x.get('node') == 'reception_v3')
    assert {'peach-9d-2027', 'peach-11d-2027'} <= evidence['provided_by_skill'].keys()


def test_rejected_draft_remains_unsent_while_customer_task_is_repaired(monkeypatch):
    from app.reception_v3 import runtime
    from test_reception_v3_skills import context, model, final
    monkeypatch.setattr(settings, 'reception_route_understanding_enabled', False)
    c = context('peach_9d_2027')
    c['messages'] = [{'id': 'latest', 'direction': 'incoming', 'content': '我只是比较住宿，没有要换线'}]
    requests = model(monkeypatch, [final(messages=[{'text': '尚未发送的草稿'}]),
                                   final(messages=[{'text': '当前问题的完整回答'}])])
    errors = iter(['需要修正报价决策字段', None])
    monkeypatch.setattr(runtime, 'quote_error', lambda *a, **k: next(errors))
    decision, logs, _ = runtime.run_agent(c)
    feedback = [json.loads(m['content']) for m in requests[1]['messages']
                if m.get('role') == 'user' and 'decision_validation_feedback' in m.get('content', '')]
    assert feedback[0]['candidate_delivered'] is False
    assert feedback[0]['original_event'] == 'customer_message'
    assert any(m.get('content') == c['messages'][0]['content'] for m in requests[1]['messages'])
    assert logs[0]['status'] == 'quote_rejected'
    assert decision['messages'] == runtime.Decision(messages=[{'text': '当前问题的完整回答'}]).model_dump()['messages']


def test_semantic_comparison_reads_other_product_without_checking_departure(library, monkeypatch):
    from app.reception_v3 import runtime
    from test_reception_v3_skills import model, final, tool
    from test_route_understanding_20261031 import ctx, result, semantic_model, NINE, ELEVEN
    c = ctx('我不换线，只比较一下两条的早餐包含吗？')
    c['profile']['departure_window'] = '2027年1月'
    for value in c['skills']['routes'].values():
        value['departure_availability'] = {'season_start': '2027-03-20', 'season_end': '2027-04-10'}
    understanding = result(c, intent='compare')
    understanding['has_question'] = True
    semantic_model(monkeypatch, [understanding])
    model(monkeypatch, [
        {'content': '', 'tool_calls': [tool('load_skill', {'name': 'peach-9d-2027'})]},
        final(route_variant=ELEVEN, messages=[{'text': '两条的酒店早餐都包含。'}])])
    monkeypatch.setattr(runtime, 'departure_quote', lambda *a, **kw: pytest.fail('A factual comparison does not request availability.'))
    decision, logs, _ = runtime.run_agent(c)
    assert decision['route_variant'] == ELEVEN and decision['route_choice'] == 'unchanged'
    assert not decision['start_introduction'] and not decision['profile'].get('_route_offer')
    assert not any(l.get('status') == 'quote_rejected' for l in logs)
