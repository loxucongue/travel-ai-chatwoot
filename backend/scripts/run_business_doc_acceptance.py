"""Exercise document questions in durable playground sessions; no live writes."""
import argparse
import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import run_advisor_playground_acceptance as qa
from app.automation_service import add_customer_message
from app.release_provenance import source_fingerprint, assert_source_unchanged, runtime_config_fingerprint

parser = argparse.ArgumentParser()
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--drive-worker', action='store_true')
args = parser.parse_args()
DEST = args.output
CHAINS = [
    ('doc_433', ['我想了解桃花9日的行程。', '這個行程容不容易高反',
                 '提前吃紅景天有效嗎？還是要去開丹木斯', '這個行程多少錢', '小費要多少錢'], 0),
    ('doc_432', ['我想了解桃花加珠峰11日的行程。', '有去納木錯嗎？', '有隨隊醫生嗎？'], 2),
    ('opening_11d', ['你好，我想諮詢旅行行程', '桃花加珠峰11日'], 2),
    ('opening_and_unknown_date', ['你好，我想諮詢旅行行程', '桃花9日', '我們2位', '還不確定日期呀', '那個時候會不會冷啊'], 1),
]

def snapshot(sid):
    with qa.SessionLocal() as db:
        s = db.get(qa.AutomationSession, sid)
        runs = db.scalars(qa.select(qa.AutomationRun).where(qa.AutomationRun.session_id == sid).order_by(qa.AutomationRun.id)).all()
        return {'messages': s.messages, 'memory': s.memory, 'journey': s.controls.get('journey'),
                'runs': [{'id': r.id, 'module': r.module, 'status': r.status,
                          'error': r.error_code, 'decision': r.decision, 'trace': r.trace} for r in runs]}

with qa.SessionLocal() as db:
    config_start = runtime_config_fingerprint(db)
    assert not qa.global_message_sending_enabled(db), 'real_sending_must_stay_off'
    before = qa._outbound_count(db)
    owner = db.scalar(qa.select(qa.User).where(qa.User.active.is_(True), qa.User.role.in_(['admin', 'super_admin'])).order_by(qa.User.id))
    inbox = db.scalar(qa.select(qa.InboxBinding).where(qa.InboxBinding.chatwoot_inbox_id == 128859))
    owner_id, inbox_id = owner.id, inbox.id

report = {'outbound_before': before, 'sessions': [], 'source_fingerprint': source_fingerprint(),
          'runtime_config_fingerprint_start': config_start}
def save():
    assert_source_unchanged(report['source_fingerprint'])
    with qa.SessionLocal() as db:
        config_end = runtime_config_fingerprint(db)
    report['runtime_config_fingerprint_end'] = config_end
    report['runtime_config_unchanged'] = (
        report.get('runtime_config_unchanged', True) and config_start == config_end
    )
    DEST.parent.mkdir(parents=True, exist_ok=True)
    DEST.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

for key, questions, touches in CHAINS:
    sid = qa._create_session(owner_id, inbox_id, {'key': key, 'title': '业务原文复验 ' + key, 'message': questions[0]})
    entry = {'scenario': key, 'session_id': sid, 'turns': []}
    report['sessions'].append(entry)
    for index, question in enumerate(questions):
        prior = snapshot(sid)
        old_ids = {m['id'] for m in prior['messages'] if m.get('direction') == 'outgoing'}
        if index:
            with qa.SessionLocal() as db:
                s = db.get(qa.AutomationSession, sid)
                add_customer_message(db, s, question, f'doc-audit:{sid}:{index}')
                db.commit()
        error = None
        try:
            qa._wait_ready(sid, drive_worker=args.drive_worker, minimum_outgoing=len(old_ids)+1, timeout=300)
        except Exception as exc:
            error = str(exc)
        current = snapshot(sid)
        outgoing = [m for m in current['messages'] if m.get('direction') == 'outgoing' and m['id'] not in old_ids]
        entry['turns'].append({'question': question, 'error': error, 'outgoing': outgoing})
        entry['state'] = current
        save()
        print(json.dumps({'session': sid, 'turn': index, 'error': error, 'text': [m.get('content') for m in outgoing if m.get('content')]}, ensure_ascii=False), flush=True)
        if error:
            break
    entry['silence_jobs'] = []
    for _ in range(touches):
        jid = qa._advance_next_silence(sid)
        if not jid:
            break
        try:
            qa._wait_silence_terminal(sid, jid, drive_worker=args.drive_worker, timeout=300)
            entry['silence_jobs'].append({'job_id': jid})
        except Exception as exc:
            entry['silence_jobs'].append({'job_id': jid, 'error': str(exc)})
            break
        entry['state'] = snapshot(sid)
        save()
with qa.SessionLocal() as db:
    report['outbound_after'] = qa._outbound_count(db)
    report['sending_enabled_after'] = qa.global_message_sending_enabled(db)
report['outbound_unchanged'] = before == report['outbound_after']
checks = {}
for entry in report['sessions']:
    key = entry['scenario']
    expected_turns = next(len(questions) for name, questions, _ in CHAINS if name == key)
    checks[key + ':all_turns_complete'] = len(entry['turns']) == expected_turns and all(t['error'] is None for t in entry['turns'])
    checks[key + ':no_failed_runs'] = all(r['status'] != 'failed' for r in entry['state']['runs'])
    for turn in entry['turns']:
        q = turn['question']
        text = '\n'.join(m.get('content', '') for m in turn['outgoing'])
        if q == '小費要多少錢':
            checks['tips_answer_not_trip_price'] = '30' in text and '9,980' not in text and '9980' not in text
        if '紅景天' in q:
            checks['medication_specific'] = '醫師' in text and '藥師' in text and ('紅景天' in text or '丹木斯' in text)
            checks['no_invented_health_history'] = '一次高反經驗' not in text
            checks['no_broken_replacement_copy'] = '對照' not in text
        if '納木錯' in q:
            checks['destination_answer_concise'] = '納木錯' in text and ('沒有' in text or '不' in text)
            checks['no_unasked_geography'] = sum(w in text for w in ['波密', '山南', '日喀則', '拉薩']) < 2
            checks['destination_no_unasked_attraction_list'] = len(text) <= 80
        if q == '我們2位':
            checks['details_no_route_repetition'] = not any(w in text for w in ['波密', '山南', '日喀則'])
        if '冷啊' in q:
            checks['weather_answers_cold'] = '冷' in text or '溫差' in text
            checks['weather_taiwan_copy'] = not any(w in text for w in ['垭', '抓絨', '衝鋒'])
        if '不確定日期' in q:
            checks['no_date_requestion'] = not any(
                phrase in text for phrase in ['幾月', '什麼時候出發', '何時出發', '哪天出發', '預計哪一天', '出發日期是']
            )
    if key.startswith('opening_'):
        first = entry['turns'][0]['outgoing']
        checks[key + ':unselected_zero_images'] = not any(m.get('media_id') for m in first)
        if len(entry['turns']) > 1:
            wave = entry['turns'][1]['outgoing']
            texts = [m.get('content') for m in wave if m.get('content')]
            checks[key + ':brand_actually_sent'] = any(
                ('全外賓' in t or '都是外賓' in t)
                and ('不拼內賓' in t or '不跟內賓' in t)
                and ('零購物' in t or '購物站' in t)
                for t in texts
            )
            checks[key + ':question_last'] = bool(texts) and texts[-1].endswith('？')
            checks[key + ':no_repeated_advisor_greeting'] = not any('China2Go' in text for text in texts)
            checks[key + ':no_city_rollcall'] = not any(sum(w in t for w in ['波密', '山南', '日喀則', '拉薩']) >= 3 for t in texts)
checks['real_sending_stays_off'] = not report['sending_enabled_after'] and report['outbound_unchanged']
report['business_checks'] = checks
report['business_passed'] = all(checks.values())
save()
print(json.dumps({'report': str(DEST), 'session_ids': [s['session_id'] for s in report['sessions']], 'outbound_unchanged': report['outbound_unchanged']}), flush=True)
raise SystemExit(0 if report['business_passed'] else 1)
