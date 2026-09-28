"""Real-model connected V3 rehearsal in an isolated database; no channel writes."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    parser.add_argument('--source-db', required=True)
    parser.add_argument('--days', type=int, choices=[9, 11], required=True)
    parser.add_argument('--budget', type=float, default=3)
    parser.add_argument('--natural', action='store_true', help='Wait for configured, unprompted follow-up')
    parser.add_argument('--explore', action='store_true', help='Start unselected, compare routes, then select')
    args = parser.parse_args()
    out = Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=True)
    if (out / 'test.db').exists():
        raise ValueError('new_output_directory_required')
    (out / 'cost.json').write_text(json.dumps({'limit_cny': min(args.budget, 18), 'calls': []}), encoding='utf-8')
    os.environ.update(APP_PROFILE='evaluation', OUTBOUND_MODE='disabled', CHATWOOT_WRITE_ENABLED='false',
        LIVE_SOP_ENABLED='false', DATABASE_URL='sqlite:///' + (out / 'test.db').as_posix(),
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
            raise RuntimeError('acceptance_network_blocked')
        return await original_async(client, request, *a, **kw)
    httpx.AsyncClient.send = send_async
    from app.main import app  # register all tables
    from app.db import Base, engine, SessionLocal
    from app.models import User, InboxBinding, utcnow
    from app.automation_models import AutomationSession, AutomationRun
    from app.reception_v3 import service
    from app.reception_v3.skills import compile_skills, SkillRegistry
    import yaml
    from sqlalchemy import select
    Base.metadata.create_all(engine)
    tables = ['tenants', 'inbox_bindings', 'knowledge_versions', 'material_assets', 'stored_media',
              'web_knowledge_sources', 'web_knowledge_revisions', 'app_settings']
    source = Path(args.source_db).resolve()
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
    with SessionLocal() as db:
        if args.natural:
            # Change only the copied test configuration. Wait real 3 + 5
            # minutes without a customer appointment or synthetic timer event.
            from app.reception_config import get_reception_configuration, SETTING_KEY
            from app.operations import save_setting
            config = get_reception_configuration(db)
            config['silence'].update(enabled=True, intervals_minutes=[3, 5])
            save_setting(db, SETTING_KEY, config)
        db.add(User(id=1, email='v3-test@example.invalid', display_name='V3验收', role='super_admin', password_hash='unused'))
        inbox = db.scalar(select(InboxBinding).order_by(InboxBinding.id))
        row = AutomationSession(owner_id=1, inbox_binding_id=inbox.id, engine_version='v3', mode='journey',
            environment='playground', messages=[], memory={}, controls={}, virtual_now=utcnow())
        db.add(row); db.flush()
        service.start(db, row, '你好，我想咨询旅行行程' if args.explore else
                      f'我想了解桃花{args.days}日' + ('含珠峰' if args.days == 11 else ''))
        sid = row.id
        bundle = compile_skills(db)
        (out / 'skills.json').write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding='utf-8')
        registry = SkillRegistry(bundle)
        for item in registry.index():
            folder = out / 'skills' / item['name']
            folder.mkdir(parents=True, exist_ok=True)
            (folder / 'SKILL.md').write_text('---\n' + yaml.safe_dump(item, allow_unicode=True, sort_keys=False)
                + '---\n\n' + registry.load(item['name'])['instructions'], encoding='utf-8')
        db.commit()
    minutes = 3 if args.days == 9 else 5
    steps = ['我們兩位，三月底出發。', '那一位自己住，要加多少？',
             '車上的氧氣跟房間的是一樣的嗎？',
             f'我先和家人討論，請{minutes}分鐘後再問我確認出發日期，現在先不要追問。',
             {'wait': minutes * 60 + 20}, '確定3月28日，請幫我找顧問核對余位。我的微信是 v3acceptance0928。']
    if args.natural:
        steps = ['我們兩位，三月底出發。', '那一位自己住，要加多少？',
                 '我先跟家人商量，主要擔心住宿，不想再看照片。',
                 {'wait': 520}, '可以用微信嗎？',
                 '我的微信是 v3natural0928，請顧問接手。']
    index, inserted, seen, seen_runs, waiting = 0, False, set(), set(), None
    preamble = ['9日和11日有什么不同？先比较一下，不用发完整介绍。',
                f'那就选桃花{args.days}日，请发完整介绍。'] if args.explore else []
    deadline = time.monotonic() + 1000
    while time.monotonic() < deadline:
        with SessionLocal() as db:
            service.tick(db, session_id=sid)
            row = db.get(AutomationSession, sid)
            for message in row.messages:
                if message.get('status') == 'simulated_delivered' and message['id'] not in seen:
                    seen.add(message['id']); emit('delivery', message=message)
            runs = db.scalars(select(AutomationRun).where(AutomationRun.session_id == sid)).all()
            for run in runs:
                if run.id not in seen_runs:
                    seen_runs.add(run.id)
                    emit('decision', status=run.status, error=run.error_code, input=run.input_snapshot,
                         decision=run.decision, trace=run.trace)
                    if run.status == 'failed':
                        raise RuntimeError(run.error_code)
            state = service.state(row)
            if not inserted and state.get('delivery_kind') == 'introduction' and any(m.get('v3_kind') == 'introduction' and m.get('status') == 'simulated_delivered' for m in row.messages):
                # Queue a related question during actual delivery. The 11d case
                # also supplies party size before the closing question is made.
                text = ('我們兩位。' if args.days == 11 else '') + '機票有包含嗎？集合在哪裡？'
                service.add_message(db, row, text, 'during-intro'); inserted = True
                emit('customer', text=text, phase='during_introduction')
            busy = state.get('pending_event') or any(m.get('status') == 'draft' for m in row.messages)
            if not busy:
                if preamble:
                    item = preamble.pop(0)
                    service.add_message(db, row, item, f'preamble-{len(preamble)}')
                    emit('customer', text=item, phase='route_matching')
                elif waiting is not None and time.monotonic() < waiting:
                    pass
                elif index < len(steps):
                    waiting = None
                    item = steps[index]; index += 1
                    if isinstance(item, dict):
                        waiting = time.monotonic() + item['wait']
                        emit('wait_started', seconds=item['wait'], next_check_at=state.get('next_check_at'))
                    else:
                        service.add_message(db, row, item, f'step-{index}')
                        emit('customer', text=item)
                else:
                    emit('completed', state=state, memory=row.memory, messages=row.messages)
                    db.commit()
                    print(json.dumps({'completed': True, 'days': args.days, 'runs': len(runs), 'handoff': state.get('handoff')}), flush=True)
                    return
            db.commit()
        time.sleep(.5)
    raise RuntimeError('acceptance_deadline_exceeded')


if __name__ == '__main__':
    main()
