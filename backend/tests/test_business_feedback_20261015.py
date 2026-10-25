"""Oct 14 original playground #631/#632 regressions; no paid model/channel calls."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.automation_api import safe_text, run_json
from app.automation_models import AutomationRun
from app.model_transport import EvaluationCallError
from app.reception_v3 import runtime, service
from app.reception_v3.seasonal_quotes import contains_trip_quote, departure_quote, validate_pricing
from app.route_packages import _validate
from scripts.configure_feedback_20261015 import update_package
from test_seasonal_retention import catalog, NOW
from test_seasonal_runtime_journeys import runtime_context, check_date, results
from test_reception_v3_skills import model, final
from test_reception_v3 import create, step
from test_tibet_release_20261014 import EIGHT, TEN


@pytest.mark.parametrize('phrase', ['LINE QR Code', 'LINE QRCode', 'LINE QR-Code', 'LINE QR_Code',
                                    'line qr code', '微信 QR Code', 'WeChat QRCode'])
def test_qr_method_is_readable_in_api_and_nested_logs(phrase):
    text = f'方便傳一下您的 {phrase} 給我嗎？'
    assert safe_text(text) == text
    assert safe_text({'messages': [{'text': text}]})['messages'][0]['text'] == text
    row = SimpleNamespace(id=1, session_id=631, module='reply', generation=1, status='completed',
                          decision={'messages': [{'text': text}]}, trace={'logs': [{'text': text}]},
                          error_code=None, created_at=NOW, completed_at=NOW)
    assert run_json(row)['decision']['messages'][0]['text'] == text


@pytest.mark.parametrize('text,secret', [('LINE ID: QRCode', 'QRCode'), ('LINE：QRCode', 'QRCode'),
    ('LINE 是 QRCode', 'QRCode'), ('LINE test.account', 'test.account'), ('LINE QRabc123', 'QRabc123'),
    ('微信 帳號: hello123', 'hello123'), ('Email a@test.example', 'a@test.example'),
    ('電話 +886912345678', '886912345678')])
def test_real_contact_values_still_redacted(text, secret):
    assert secret not in safe_text(text)


def test_explicit_contact_fields_still_mask_generic_looking_values():
    assert safe_text({'line_id': 'QRCode'}) == {'line_id': '[联系方式已脱敏]'}
    assert safe_text('2026-10-15T01:00:00Z') == '2026-10-15T01:00:00Z'
    assert safe_text('人民幣12,180元/人') == '人民幣12,180元/人'


@pytest.mark.parametrize('route,start,end', [(EIGHT, '2026-12-01', '2027-02-28'),
    (EIGHT, '2027-12-16', ''), (TEN, '2027-01-01', '2027-01-31')])
def test_winter_advice_survives_unpublished_year_without_becoming_saleable(route, start, end):
    result = departure_quote(catalog(), route, start, end, NOW)
    assert result['status'] == 'unavailable'
    assert result['next_action'] == 'offer_winter_recommendation'
    recommendation = result['winter_recommendation']
    assert '關閉' in recommendation['explanation'] and '珠峰小鎮' in recommendation['explanation']
    assert '冰川' in recommendation['name']
    assert not any(k in result for k in ('price_cny', 'script_id', 'departure_examples'))
    assert result['alternatives'] == []


@pytest.mark.parametrize('start,end', [('2027-06-01','2027-06-30'), ('2027-04-21','2027-04-24'),
                                      ('2026-12-01','2027-03-31'), ('2028-06-01','2028-06-30')])
def test_winter_facts_not_attached_to_nonwinter_or_mixed_windows(start, end):
    assert 'winter_recommendation' not in departure_quote(catalog(), EIGHT, start, end, NOW)


def test_original_631_tool_call_and_winter_followup_enforcement(monkeypatch):
    c = runtime_context(EIGHT)
    c['messages'] = [{'direction': 'incoming', 'content': '西藏精華8天・拉薩＋珠峰'},
                     {'direction': 'outgoing', 'content': '請問您大概想幾月出發呢？'},
                     {'direction': 'incoming', 'content': '12-2月'}]
    reply = '這段時間絨布旅館關閉，需要住山下珠峰小鎮。我們另有季節限定冰川行程，您想先了解嗎？'
    requests = model(monkeypatch, [check_date(EIGHT, '2026-12-01', '2027-02-28'),
        final(route_variant=EIGHT, interrupt=True, messages=[{'text': reply}])])
    decision, _, _ = runtime.run_agent(c)
    assert results(requests[1])[-1]['next_action'] == 'offer_winter_recommendation'
    assert decision['messages'][0]['text'] == reply
    c['profile'] = decision['profile']; c['event'] = 'silence_due'
    requests = model(monkeypatch, [final(route_variant=EIGHT, messages=[{'text': '常規珠峰照片', 'asset_keys': ['everest']}]),
        final(route_variant=EIGHT, interrupt=True, messages=[{'text': '如果想了解冬季冰川的安排，隨時跟我說就好。'}])])
    decision, logs, _ = runtime.run_agent(c)
    assert len(requests) == 2 and logs[0]['status'] == 'quote_rejected'
    assert decision['messages'][0]['asset_keys'] == []
    # A new customer date/tool result replaces the winter restriction.
    c['event'] = 'customer_message'
    requests = model(monkeypatch, [check_date(EIGHT, '2027-06-01', '2027-06-30'),
        final(route_variant=EIGHT, start_introduction=True)])
    decision, _, _ = runtime.run_agent(c)
    assert decision['profile']['_departure_check']['result']['status'] == 'quoted'


@pytest.mark.parametrize('text', [
    '4位獨立成團需在原團費基礎上每人加人民幣400元。',
    '4人独立成团在原团费基础上每人加人民币400元。',
    '4位獨立成團原團費每人加400元。', '原團費另加人民幣400元。',
    '4位獨立成團加價合計人民幣1,600元。'])
def test_private_supplement_is_not_total_tour_price(text):
    assert not contains_trip_quote(text)


@pytest.mark.parametrize('text', ['團費400元', '獨立成團總價人民幣8,380元',
    '原團費每人加400元，總價人民幣8,380元。', '獨立成團加價400元，團費人民幣7,980元。'])
def test_total_quotes_are_still_checked(text):
    assert contains_trip_quote(text)


def test_original_632_private_group_followup_reaches_delivery_without_date_retry(session_factory, monkeypatch):
    c = runtime_context(EIGHT)
    c['profile'] = {'party_size': 4, 'departure_window': '3-4月', 'elder_note': '爸爸68歲'}
    c['messages'] = [{'direction': 'outgoing', 'content': '您們4位也可以考慮自己一團出發喔～'},
                     {'direction': 'incoming', 'content': '4個人走要不要加錢'}]
    script = next(s for s in c['skills']['routes'][EIGHT]['scripts'] if s['id'] == 'private_group_supplement')
    requests = model(monkeypatch, [final(route_variant=EIGHT, messages=[{'script_id': script['id']}])])
    decision, _, _ = runtime.run_agent(c)
    assert len(requests) == 1
    with session_factory() as db:
        row = create(db, monkeypatch)
        _, parts = service.parts_for(db, row, c['skills'], decision)
        assert len(parts) == 1 and parts[0]['content'] == script['answer_text']
        assert '每人加人民幣400元' in parts[0]['content'] and '拼團不適用' in parts[0]['content']


def test_quote_failure_keeps_candidate_and_guard_reason(monkeypatch):
    c = runtime_context(EIGHT)
    bad = final(route_variant=EIGHT, messages=[{'text': '團費人民幣7,980元'}])
    model(monkeypatch, [bad, bad])
    with pytest.raises(EvaluationCallError) as captured:
        runtime.run_agent(c)
    assert len(captured.value.logs) == 2
    assert all(log['validation_error'] and log['rejected_decision'] for log in captured.value.logs)


def test_three_failures_simulate_silent_handoff_and_preserve_failure_evidence(session_factory, monkeypatch):
    def fail(c):
        raise EvaluationCallError('seasonal_quote_not_validated', [{'validation_error': 'original reason'}], 'evidence-hash')
    with session_factory() as db:
        row = create(db, monkeypatch)
        monkeypatch.setattr(service, 'run_agent', fail)
        step(db, row); step(db, row)
        for _ in range(2):
            value = service.state(row)
            value['retry_at'] = '2000-01-01T00:00:00Z'
            service.save(row, value); row.due_at = value['retry_at']; db.commit()
            step(db, row)
        runs = list(db.scalars(select(AutomationRun)))
        assert len(runs) == 3 and all(r.status == 'failed' for r in runs)
        assert all(r.trace['logs'] == [{'validation_error': 'original reason'}] for r in runs)
        assert all(r.trace['request_hash'] == 'evidence-hash' for r in runs)
        assert service.state(row)['handoff'] and row.controls['human']
        assert row.due_at is None and not any(m.get('status') == 'draft' for m in row.messages)
        assert not any('收到' in m.get('content', '') for m in row.messages)


@pytest.mark.parametrize('route', [EIGHT, TEN])
def test_configuration_update_preserves_prices_assets_and_operator_edits(route):
    original = deepcopy(catalog()['routes'][route])
    original['seasonal_pricing'].pop('winter_recommendation')
    original['fixed_answers'] = [s for s in original['fixed_answers'] if s['id'] != 'private_group_supplement']
    original['ai_guidance'] = '保留运营修改'
    updated = update_package(original)
    assert updated['ai_guidance'].startswith(original['ai_guidance'])
    for k in original.keys() - {'seasonal_pricing', 'fixed_answers', 'ai_guidance'}:
        assert original[k] == updated[k]
    assert original['seasonal_pricing'] == {k:v for k,v in updated['seasonal_pricing'].items() if k != 'winter_recommendation'}
    assert updated['fixed_answers'][:-1] == original['fixed_answers']
    assert update_package(updated) == updated
    _validate(updated, __import__('pathlib').Path('test.json'))
    updated['seasonal_pricing']['winter_recommendation']['months'] = [6]
    with pytest.raises(ValueError): validate_pricing(updated['seasonal_pricing'])
