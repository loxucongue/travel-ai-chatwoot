"""Run connected customer journeys against installed code, in a separate DB.

This script never connects to Chatwoot. Only model HTTP calls are permitted.
The source DB supplies configuration/materials, never customer conversations.
Wait steps use wall time and the normal playground scheduler at speed 1.
"""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import uuid


def scenarios(suite, minutes):
    if suite == 'appointment':
        route = '9日' if minutes == 3 else '11日含珠峰'
        return {'appointment': [f'我想了解桃花{route}。',
            f'人數還沒定，先不用發整套。請{minutes}分鐘後再跟我介紹住宿，現在先不用講。',
            {'wait': 315, 'label': 'scheduled_hotel'},
            '收到，波密那晚也一樣嗎？', '機票有包含嗎？', '我們兩位，可以介紹完整行程。',
            '其中一位自己住，要加多少？', '可以微信聯絡嗎？',
            f'我的微信是 acceptance_time_{minutes}_0928，請顧問接手。']}
    if suite == 'silence':
        route = '9日' if minutes == 3 else '11日含珠峰'
        return {'silence': [f'我想了解桃花{route}。',
            ('人數還沒定，先不用發整套，我對拉薩的寺廟文化有興趣。' if minutes == 3 else
             '人數還沒定，我在意車子和住宿舒不舒服，先講車子就好，住宿後面再聊，不用發整套。'),
            {'wait': 315, 'label': 'relevant_new_value'},
            '收到，拉薩還有哪些文化景點？', '我們兩位，可以開始介紹整套行程。',
            '我們是朋友，3月底出發。', '第一天在哪裡集合？最後一天有送機嗎？',
            '車子的座椅和空間會很擠嗎？', '我先和朋友商量，主要在意車子舒不舒服。',
            {'wait': 315, 'label': 'considering'},
            '先不要再主動聯絡，我有需要會自己問。', '我回來確認一下，機票包含在團費嗎？',
            {'wait': 315, 'label': 'optout_after_question'}]}
    return {
        'nine': ['我想了解桃花9日，不上珠峰的行程。', '我們兩位，先看完整行程。',
            '住宿都是希爾頓嗎？團費有包含機票嗎？', '那波密那晚也是嗎？', '回程要自己去機場嗎？',
            '改成4位，另外兩個朋友也來，3月底出發。', '一位自己住一間，要加多少？',
            '高原注意事項PDF和車子的照片都給我，兩個都要。', '我們討論好了，下一步怎麼安排？',
            '用微信聯絡就好。', '我的微信是 acceptance_9d_0928，請顧問接著幫我們確認。'],
        'eleven_queue': ['我點的是桃花珠峰11日，我們4位。',
            {'interrupt': '先繼續發行程，我想問珠峰那晚有獨立衛浴嗎？團費含機票嗎？'},
            '那間房內有供氧嗎？', '團費是每個人多少錢？', '如果一位自己住，要加多少？',
            '車子的座椅和空間會很擠嗎？', '入藏函在哪裡拿？', '我不想留LINE，先在這裡聊就好。',
            '最後一天有送機嗎？', '我的微信是 acceptance_11d_0928，請真人接著幫我們確認。'],
        'matching': ['你好，想了解西藏旅遊。', '我們兩位，第一次去，想看桃花，也想知道珠峰差在哪。',
            '只有9到11天，不想太趕，住宿也希望好一點。', '先比較住宿和每天走法，還沒選定。',
            '就9日不上珠峰的，兩位。', '全部介紹看完了，那波密住宿和拉薩一樣嗎？',
            '3月底出發，團費包含哪些？', '其中一位自己住要加多少？', '可以微信聯絡嗎？',
            '我的微信是 acceptance_match_0928，請顧問接手。'],
        'switch': ['我想看桃花9日，我們3位。', '11日多了珠峰，那邊住宿跟9日一樣嗎？先比較，不是改線。',
            '想好了，改成11日含珠峰，還是3位。', '3月26日想出發。', '更正，是3月27日。',
            '珠峰那晚有獨立衛浴嗎？', '另外一位自己住一間，要加多少？', '確認一下，我們現在選哪條，幾位，哪天？',
            '機票可以協助代訂嗎？我只是先問，還不用查。', '先和朋友商量一下，不急。'],
        'refusal': ['9日，兩位，先看看行程。', '團費有含正餐和小費嗎？',
            '機票可以幫忙訂嗎？我只是先問問，還不用查。', '我不想留LINE，先在這裡聊就好。',
            '第一天在林芝接機對嗎？', '我先和家人商量，主要考慮接送方便不方便。',
            {'wait': 315, 'label': 'considering'}, '先不要再主動聯絡，有需要我自己問。',
            '我回來問一下，最後一天拉薩有送機嗎？', '那天從拉薩坐火車也能送嗎？',
            {'wait': 315, 'label': 'optout'}],
        'materials': ['桃花珠峰11日，兩位，想看完整行程。', '高原注意事項PDF和車子的照片都給我，兩個都要。',
            '照片這台車的氧氣，跟珠峰房間的設備是一樣的嗎？', '所以車上跟房間要分開看，對嗎？',
            '如果一人一間，房差要另外多少？', '入藏函通常在哪裡拿？', '成都不是集合地點對嗎？',
            '我们改成3位，还是11日。', '3月28日還有沒有空位？請真人接手核對，我的LINE是 acceptance_pdf_0928。'],
        'stop_intro': ['桃花9日，兩位，請介紹。', {'interrupt': '先不要再發介紹，也不要主動聯絡我。'},
            '我自己回來問一下，小費包含在團費嗎？', {'wait': 315, 'label': 'stop_during_introduction'}],
        'human_intro': ['桃花珠峰11日，我們4位。', {'interrupt': '請真人接手，不要繼續發介紹了。'}],
        'switch_intro': ['桃花9日，我們兩位。', {'interrupt': '改成11日含珠峰的，還是兩位。'},
            '現在是11日對嗎？珠峰那晚有獨立衛浴嗎？'],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    parser.add_argument('--source-db', required=True)
    parser.add_argument('--suite', choices=['main', 'silence', 'appointment'], default='main')
    parser.add_argument('--minutes', type=int, choices=[3, 5], default=3)
    parser.add_argument('--budget', type=float, default=8)
    parser.add_argument('--case', help='Comma-separated named journeys for a focused retest')
    args = parser.parse_args()
    out = Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=True)
    if (out / 'test.db').exists():
        raise RuntimeError('use_a_new_output_directory')
    source = Path(args.source_db).resolve()
    assert source.is_file() and source != out / 'test.db'
    (out / 'cost.json').write_text(json.dumps({'limit_cny': min(18, args.budget), 'calls': []}), encoding='utf-8')
    os.environ.update(APP_PROFILE='evaluation', OUTBOUND_MODE='disabled', CHATWOOT_WRITE_ENABLED='false',
        LIVE_SOP_ENABLED='false', AI_ENGINE_DEFAULT='v2', DATABASE_URL='sqlite:///' + (out / 'test.db').as_posix(),
        MODEL_TEST_COST_LEDGER=str(out / 'cost.json'))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    os.chdir(Path(__file__).resolve().parents[1])
    import httpx
    from app.config import settings
    original = httpx.Client.send
    def send(client, request, *a, **kw):
        if str(request.url) != settings.deepseek_base_url.rstrip('/') + '/chat/completions' or request.method != 'POST':
            raise RuntimeError('acceptance_network_blocked')
        return original(client, request, *a, **kw)
    httpx.Client.send = send
    original_async = httpx.AsyncClient.send
    async def send_async(client, request, *a, **kw):
        if str(request.url) != settings.deepseek_base_url.rstrip('/') + '/chat/completions' or request.method != 'POST':
            raise RuntimeError('acceptance_async_network_blocked')
        return await original_async(client, request, *a, **kw)
    httpx.AsyncClient.send = send_async
    from app.main import app
    from app.db import Base, engine, SessionLocal
    from app.models import User, AppSetting, InboxBinding, OutboundMessage, utcnow
    from app.automation_models import AutomationSession, AutomationRun, RehearsalJob, RehearsalEnrollment
    from app.automation_service import start_open_journey, add_customer_message, advance_running_playgrounds, queue_passive, process_automation_run
    from app.reception_v2 import ENGINE_RELEASE_ID
    from sqlalchemy import select, func
    Base.metadata.create_all(engine)
    tables = ['tenants', 'inbox_bindings', 'knowledge_versions', 'material_assets', 'stored_media',
              'sop_definitions', 'sop_versions', 'reply_policies', 'web_knowledge_sources', 'web_knowledge_revisions', 'app_settings']
    with sqlite3.connect(f'file:{source.as_posix()}?mode=ro', uri=True) as src, sqlite3.connect(out / 'test.db') as dst:
        src.row_factory = sqlite3.Row
        for table in tables:
            where = " where key in ('route_reception_config','global_message_sending')" if table == 'app_settings' else ''
            for row in src.execute('select * from ' + table + where):
                cols = list(row.keys())
                dst.execute('insert into ' + table + ' (' + ','.join('"'+k+'"' for k in cols) + ') values (' + ','.join('?' for _ in cols) + ')', tuple(row))
    def emit(kind, **values):
        with (out / 'events.jsonl').open('a', encoding='utf-8') as f:
            f.write(json.dumps({'wall': utcnow(), 'kind': kind, **values}, ensure_ascii=False, default=str) + '\n')
    from app.reception_v2 import runtime
    validate_decision = runtime._validated_decision
    def record_decision(message, *a, **kw):
        # Synthetic acceptance inputs only; preserve failed drafts for diagnosis.
        with (out / 'model-drafts.jsonl').open('a', encoding='utf-8') as f:
            f.write(json.dumps(message, ensure_ascii=False) + '\n')
        return validate_decision(message, *a, **kw)
    runtime._validated_decision = record_decision
    with SessionLocal() as db:
        db.add(User(id=1, email='acceptance@example.invalid', display_name='连续验收', role='super_admin', password_hash='unused'))
        db.get(AppSetting, 'global_message_sending').value = {'enabled': False}
        row = db.get(AppSetting, 'route_reception_config')
        silence = {**row.value.get('silence', {}), 'active_start': '00:00', 'active_end': '23:59'}
        if args.suite in {'silence', 'appointment'}:
            silence['v2_intervals_minutes'] = [args.minutes, 120]
        row.value = {**row.value, 'silence': silence}
        db.commit()
        emit('baseline', release=ENGINE_RELEASE_ID, model=settings.deepseek_model,
             configuration=row.value, source_db=str(source), outbound=False)
    plans = scenarios(args.suite, args.minutes)
    if args.case:
        plans = {key: plans[key] for key in args.case.split(',')}
    cases = {}
    with SessionLocal() as db:
        inbox = db.scalar(select(InboxBinding).where(InboxBinding.chatwoot_inbox_id == 128859))
        for key, steps in plans.items():
            s = AutomationSession(owner_id=1, inbox_binding_id=inbox.id, mode='journey', environment='playground',
                engine_version='v2', engine_release_id=ENGINE_RELEASE_ID, virtual_now=utcnow(), messages=[],
                controls={'can_reply': True, 'ai_enabled': True, 'channel': 'facebook', 'labels': [], 'human': False,
                    'permission_source': 'simulated', 'history_complete': True, 'acceptance_scenario': key})
            db.add(s); db.flush()
            start_open_journey(db, s, duration_minutes=1440, speed_multiplier=1, entry_message=steps[0])
            cases[key] = {'id': s.id, 'index': 1, 'seen': set(), 'wait': None}
            emit('customer', key=key, text=steps[0])
        db.commit()
    deadline = time.monotonic() + 2400
    while cases:
        if time.monotonic() > deadline:
            raise RuntimeError('acceptance_deadline_exceeded')
        for key, case in list(cases.items())[:1]:
            with SessionLocal() as db:
                sid = case['id']
                advance_running_playgrounds(db, session_id=sid); db.commit()
                queue_passive(db, environment='playground', session_id=sid)
                process_automation_run(db, environment='playground', session_id=sid)
                advance_running_playgrounds(db, session_id=sid); db.commit()
                s = db.get(AutomationSession, sid); db.refresh(s)
                runs = db.scalars(select(AutomationRun).where(AutomationRun.session_id == sid)).all()
                jobs = db.scalars(select(RehearsalJob).join(RehearsalEnrollment,
                    RehearsalJob.enrollment_id == RehearsalEnrollment.id).where(RehearsalEnrollment.session_id == sid)).all()
                for message in s.messages:
                    identity = (message.get('id'), message.get('status'))
                    if message.get('direction') == 'outgoing' and message.get('status') != 'draft' and identity not in case['seen']:
                        case['seen'].add(identity); emit('delivery', key=key, message=message)
                (out / (key + '.json')).write_text(json.dumps({'id': sid, 'controls': s.controls, 'messages': s.messages,
                    'jobs': [{'id': j.id, 'status': j.status, 'scheduled_at': j.scheduled_at,
                              'confirmed_at': j.confirmed_at, 'reason': j.reason} for j in jobs],
                    'runs': [{'id': r.id, 'status': r.status, 'error_code': r.error_code, 'input': r.input_snapshot,
                              'decision': r.decision, 'trace': r.trace} for r in runs]}, ensure_ascii=False, default=str), encoding='utf-8')
                failed = [r.id for r in runs if r.status in {'failed', 'blocked'}]
                if failed:
                    emit('failed', key=key, run_ids=failed)
                    raise RuntimeError('acceptance_run_failed')
                busy = bool(s.due_at or any(r.status in {'pending', 'processing'} for r in runs)
                            or any(m.get('status') == 'draft' for m in s.messages))
                if case['index'] == len(plans[key]):
                    if not busy:
                        emit('completed', key=key); del cases[key]
                    continue
                step = plans[key][case['index']]
                if isinstance(step, dict) and 'interrupt' in step:
                    if not any(m.get('status') == 'simulated_delivered' and m.get('media_id') for m in s.messages):
                        continue
                    if not busy:
                        raise RuntimeError('interruption_missed_introduction')
                    text = step['interrupt']
                elif busy:
                    continue
                elif isinstance(step, dict):
                    if case['wait'] is None:
                        case['wait'] = time.monotonic()
                        case['checks'] = [60, 180, 300]
                        emit('wait_started', key=key, label=step['label'], seconds=step['wait'])
                    elapsed = time.monotonic() - case['wait']
                    for checkpoint in list(case['checks']):
                        if elapsed >= checkpoint:
                            case['checks'].remove(checkpoint)
                            emit('silence_observation', key=key, label=step['label'], elapsed=elapsed, checkpoint=checkpoint)
                    if time.monotonic() - case['wait'] < step['wait']:
                        continue
                    emit('wait_completed', key=key, label=step['label'], elapsed=time.monotonic()-case['wait'])
                    case['wait'] = None; case['index'] += 1
                    continue
                else:
                    text = step
                add_customer_message(db, s, text, 'acceptance-' + uuid.uuid4().hex); db.commit()
                emit('customer', key=key, text=text); case['index'] += 1
        time.sleep(.5)
    with SessionLocal() as db:
        count = db.scalar(select(func.count()).select_from(OutboundMessage))
        assert count == 0
        emit('finished', outbound=count)


if __name__ == '__main__':
    main()
