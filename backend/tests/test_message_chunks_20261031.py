from copy import deepcopy
import re

import pytest

from app.reception_v3.message_chunks import text_chunks, split_parts
from app.reception_v3 import service
from test_reception_v3 import create, step, answer, delivered


LONG = ('八月想上珠峰，我會建議走10天這條：從林芝進藏，先走林芝、魯朗，再到拉薩，身體有時間慢慢適應。'
        '車上有血氧儀和氧氣設備，途中真的不舒服會協助聯絡醫院、安排就醫。'
        '這些是途中支援，不能保證完全不會有高原反應，行前還是建議請醫師依您的身體狀況評估。'
        '先給您看這條10天的行程圖，您感受一下節奏。您們這趟大概幾位一起出發呢？')


def compact(text):
    return re.sub(r'\s+', '', text)


def test_screenshot_style_long_answer_keeps_all_content_and_complete_sentences():
    chunks = text_chunks(LONG)
    assert 2 <= len(chunks) <= 4
    assert compact(''.join(chunks)) == compact(LONG)
    assert all(c[-1] in '。？' for c in chunks)
    assert all(len(c) <= 140 for c in chunks)
    assert '不能保證完全不會有高原反應' in ''.join(chunks)


@pytest.mark.parametrize('text', [
    '您好，名額需要確認。',
    '長' * 200,  # No safe sentence boundary; never truncate.
    '報價：¥12,880.50／人\n包含：' + '住宿、早餐、門票、' * 22,
    '請看 https://example.com/a?days=10&rate=12880.50!x=y ' + '說明' * 90,
    '「' + '引文。' * 50 + '」',
    '（' + '附註。' * 50 + '）',
    '```\n' + 'code!?。\n' * 30 + '```',
])
def test_short_text_and_indivisible_content_are_unchanged(text):
    assert text_chunks(text) == [text]


def test_quotes_and_repeated_punctuation_stay_with_the_sentence():
    quoted = '我們的說明是「' + '內容' * 65 + '。」'
    text = quoted + '下一句！！還有一段說明。'
    assert text_chunks(text) == [quoted, '下一句！！還有一段說明。']


def test_link_query_and_decimal_are_intact_without_blocking_later_sentences():
    first = '行程說明' * 30 + '請看[官網](https://example.com/a?price=12880.50&days=10)。'
    last = '下一段。' * 5
    assert text_chunks(first + last) == [first, last]


def test_parts_preserve_media_metadata_and_only_last_chunk_has_final_delay():
    parts = [{'content': LONG,
              'interval_seconds_after': 60, 'material_rule_id': 'r1'},
             {'content_type': 'image', 'asset_key': 'map'}]
    before = deepcopy(parts)
    result = split_parts(parts)
    assert parts == before
    assert result[-1] == parts[-1]
    assert all(p['material_rule_id'] == 'r1' for p in result[:-1])
    assert 'interval_seconds_after' not in result[0]
    assert result[-2]['interval_seconds_after'] == 60


@pytest.mark.parametrize('deliver', [False, True])
def test_contact_invitation_remains_atomic_for_delivered_history(deliver):
    from types import SimpleNamespace
    from app.reception_v3.lead_priority import task
    preface = ('進藏行程需要準備的資料比較多，我們可以提供詳細行程和費用包含的說明，方便您和家人一起比較。'
               '您目前還在考慮，可以先看每天的安排，等確定想了解的部分再問我們；不需要現在就決定報名。'
               '之後對交通、住宿或文件有問題，也可以繼續在這裡詢問，我們會依照您的需求提供已有資料。')
    invitation = preface + '方便留一下您的 LINE ID 或 QR Code 嗎？'
    row = SimpleNamespace(messages=[], virtual_now='2026-10-31T01:00:00+00:00', controls={})
    state = {}
    service.append_plan(row, state, [{'content': invitation, 'contact_invitation': True,
                                     'contact_channels': ['line'], 'proactive': True}], 'reply', 2, 17)
    service.save(row, state)
    if deliver:
        service.complete_delivery(row, row.messages[0]['id'], 'simulated_delivered')
    # A customer reply stops all remaining proactive drafts.
    row.messages = [{**m, 'status': 'cancelled'} if m['status'] == 'draft' else m for m in row.messages]
    history = [{'id': 'q1', 'content': '想看行程', 'direction': 'incoming',
                'created_at': '2026-10-30T01:00:00+00:00'}, *row.messages]
    context = {'event': 'silence_due', 'now': '2026-11-01T01:00:00+00:00', 'messages': history,
               'skills': {'lead_capture': {'enabled': True, 'next_day_invitation_enabled': True,
                                          'require_supported_route': False}}}
    sent = [m for m in row.messages if m['status'] == 'simulated_delivered']
    assert (task(context) is None) == deliver
    assert any('LINE ID' in m['content'] for m in sent) == deliver
    assert len(row.messages) == 1
    assert row.messages[0]['contact_channels'] == ['line']


def test_split_reply_delivers_in_order_then_handoff_cancels_remaining(session_factory, monkeypatch):
    monkeypatch.setattr(service, 'run_agent', lambda c: answer(messages=[{'text': LONG}]))
    with session_factory() as db:
        row = create(db, monkeypatch)
        step(db, row); step(db, row)
        draft = [m for m in row.messages if m.get('status') == 'draft']
        assert [m['content'] for m in draft] == text_chunks(LONG)
        assert len({m['plan_id'] for m in draft}) == 1
        assert len({m['id'] for m in draft}) == len(draft)
        step(db, row)
        assert draft[0]['content'] in delivered(row)
        assert draft[-1]['content'] not in delivered(row)
        monkeypatch.setattr(service, 'run_agent', lambda c: answer(action='handoff', interrupt=True, handoff_reason='客戶要求真人'))
        service.add_message(db, row, '先不要再發，請真人接手', 'human'); db.commit()
        step(db, row, 0)
        assert not any(m.get('status') == 'draft' for m in row.messages)
        assert draft[-1]['content'] not in delivered(row)
        assert service.state(row)['handoff']


def test_split_plan_preserves_delay_before_picture_and_completion():
    from types import SimpleNamespace
    row = SimpleNamespace(messages=[], virtual_now='2026-10-31T00:00:00+00:00', controls={})
    state = {}
    service.append_plan(row, state, [{'content': LONG}, {'content_type': 'image', 'asset_key': 'map'}], 'reply', 2, 17)
    service.save(row, state)
    assert [m.get('content') for m in row.messages[:-1]] == text_chunks(LONG)
    for part in row.messages:
        service.complete_delivery(row, part['id'], 'simulated_delivered')
    assert not service.state(row).get('delivery_kind')
    assert all(m['run_id'] == 17 for m in row.messages)


def test_availability_wording_fact_and_source_reach_model(monkeypatch):
    import hashlib,json
    from app.reception_v3.skills import service_knowledge
    from app.route_packages import PACKAGE_ROOT
    from app.reception_v3 import runtime
    from test_reception_v3_skills import context,model,final
    knowledge=service_knowledge()
    source=next(s for s in knowledge['sources'] if s['id']=='business.feedback.20261031')
    assert hashlib.sha256((PACKAGE_ROOT.parent/'service-knowledge'/source['snapshot_path']).read_bytes()).hexdigest()==source['snapshot_sha256']
    ctx=context('peach_11d_2027');ctx['skills']['service_knowledge']=knowledge
    requests=model(monkeypatch,[final(messages=[{'text':'名額需要確認。'}])])
    runtime.run_agent(ctx)
    payload=json.dumps(requests[0],ensure_ascii=False)
    for expected in ('service.everest_availability_wording','3/20–10/19','10/20–次年3/19','独团','不含珠峰过夜','business.feedback.20261031'):
        assert expected in payload
