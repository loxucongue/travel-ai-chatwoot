"""Run linked reception journeys in memory; only the model endpoint may be called.

Uses a read-only production configuration/media snapshot. Media resolution is
simulated, but intake, buffering, delivery order, timing and decisions use V3.
"""
import argparse
from copy import deepcopy
import json
import logging
import os
from pathlib import Path
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--cases', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--ledger', type=Path, required=True)
    parser.add_argument('--workers', type=int, choices=(1, 2), default=2)
    args = parser.parse_args()
    args.output = args.output.resolve()
    snapshot = json.loads(args.snapshot.read_text(encoding='utf-8-sig'))
    cases = json.loads(args.cases.read_text(encoding='utf-8-sig'))
    args.output.mkdir(parents=True, exist_ok=True)
    os.environ.update(APP_PROFILE='evaluation', OUTBOUND_MODE='disabled', CHATWOOT_WRITE_ENABLED='false',
                      LIVE_SOP_ENABLED='false', DATABASE_URL='sqlite://', RELAY_BASE_URL='', RELAY_API_TOKEN='',
                      MODEL_TEST_COST_LEDGER=str(args.ledger.resolve()))
    backend = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(backend))
    os.chdir(backend)
    from app.config import settings
    import httpx
    original = httpx.Client.send
    original_async = httpx.AsyncClient.send
    allowed = settings.deepseek_base_url.rstrip('/') + '/chat/completions'
    def guard(client, request, *a, **kw):
        if request.method != 'POST' or str(request.url) != allowed:
            raise RuntimeError('effect_test_external_write_blocked')
        return original(client, request, *a, **kw)
    async def guard_async(client, request, *a, **kw):
        if request.method != 'POST' or str(request.url) != allowed:
            raise RuntimeError('effect_test_external_write_blocked')
        return await original_async(client, request, *a, **kw)
    httpx.Client.send, httpx.AsyncClient.send = guard, guard_async
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session
    from app.main import app  # Register all table metadata, without starting the app.
    logging.getLogger('httpx').setLevel(logging.WARNING)
    from app.db import Base
    from app.models import Tenant, User
    from app.automation_models import AutomationSession, AutomationRun
    from app.reception_v3 import service
    bundle = snapshot['bundle']
    service.compile_skills = lambda db: deepcopy(bundle)
    service.candidate_materials = lambda *a: deepcopy(snapshot['materials'])
    service.active_web_facts = lambda *a, **kw: (snapshot['website_facts'], snapshot['website_version'])
    service.opening_media_info = lambda db, item, tenant: deepcopy(snapshot['opening_media'][str(item['media_id'])])
    materials = {m['key']: m for m in snapshot['materials']}
    def resolve(db, keys, route, tenant):
        result = []
        for key in keys:
            m = materials[key]
            assert route in m['routes'], (key, route)
            result.append({**m, 'asset_key': key, 'content': '', 'content_type': m['content_type'], 'route_variant': route})
        return result
    service.resolve_materials = resolve

    def run_case(case):
        engine = create_engine('sqlite://')
        Base.metadata.create_all(engine)
        records = []
        with Session(engine, expire_on_commit=False) as db:
            db.add(Tenant(id=1, name='isolated-effect-test'))
            db.add(User(id=1, email='test@example.com', display_name='Test', password_hash='unused', role='super_admin'))
            db.flush()
            row = AutomationSession(owner_id=1, engine_version='v3', mode='journey', environment='playground',
                messages=[], memory={}, controls={}, virtual_now=case.get('now', '2026-10-11T02:00:00+00:00'))
            db.add(row); db.flush()
            service.start(db, row, case['entry']); db.commit()
            def tick(seconds=0):
                row.virtual_now = service.later(row.virtual_now, seconds)
                wall = row.controls['simulation']['last_wall_at']
                service.advance(row, wall_now=wall)
                db.commit()
                service.tick(db, session_id=row.id, advance_clock=False, wall_now=wall)
                db.refresh(row)
                if service.state(row).get('failed_event'):
                    raise RuntimeError(service.state(row)['last_error'])
            def drain():
                for _ in range(90):
                    state = service.state(row)
                    if state.get('pending_event'):
                        if state.get('retry_at'):
                            time.sleep(max(0, min(8, (service.date(state['retry_at'])-service.date(service.utcnow())).total_seconds())))
                        tick()
                    elif state.get('delivery_due_at'):
                        tick(max(0, (service.date(state['delivery_due_at'])-service.date(row.virtual_now)).total_seconds()))
                    else:
                        return
                raise RuntimeError('delivery_did_not_settle')
            try:
                entry = {'id':'entry', 'checks': (['intake'] if case.get('known_entry',True) else []) + case.get('entry_checks',[])}
                for i, step in enumerate([entry] + case['steps']):
                    before = {m['id'] for m in row.messages if m.get('status') == 'simulated_delivered'}
                    if step.get('text'):
                        service.add_message(db, row, step['text'], f'customer-{i}',
                                            step.get('content_type','text'), step.get('attachments'))
                        db.commit()
                    seconds = step.get('seconds',0)
                    if step.get('next_check'):
                        target = service.state(row).get('next_check_at')
                        if not target:raise RuntimeError('expected_followup_missing')
                        seconds = max(0,(service.date(target)-service.date(row.virtual_now)).total_seconds())
                    tick(seconds)
                    if step.get('drain',True):
                        drain()
                    emitted = [m for m in row.messages if m.get('status')=='simulated_delivered' and m['id'] not in before]
                    text = '\n'.join(m.get('content','') for m in emitted if m.get('content'))
                    state = service.state(row)
                    failures=[]
                    for check in step.get('checks',[]):
                        if check=='intake' and (not emitted or emitted[0]['content']!=bundle['intake']['question']):failures.append(check)
                        if check=='no_send' and emitted:failures.append(check)
                        if check=='no_contact' and re.search(r'LINE|QR\s*Code|Email|信箱',text,re.I):failures.append(check)
                        if check=='no_price' and re.search(r'12,?180|12,?780|10,?780|單房差.*\d|加\s*400',text):failures.append(check)
                        if check=='handoff' and not state.get('handoff'):failures.append(check)
                        if check=='not_handoff' and state.get('handoff'):failures.append(check)
                        if check=='opt_out' and not state.get('opt_out'):failures.append(check)
                        if check=='send' and not emitted:failures.append(check)
                        if check=='one_new_photo':
                            photos = [m for m in emitted if m.get('content_type') == 'image']
                            previous_hashes = {m.get('media_hash') for m in row.messages if m['id'] in before and m.get('media_hash')}
                            if (len(photos) != 1 or not photos[0].get('media_id')
                                    or not photos[0].get('media_hash') or photos[0]['media_hash'] in previous_hashes
                                    or photos[0].get('route_variant') != row.controls.get('route_variant')):
                                failures.append(check)
                        if check=='text_only' and any(m.get('content_type') != 'text' for m in emitted):failures.append(check)
                        if check=='no_third_person' and re.search(r'顧問可以|請顧問|讓顧問|轉人工|轉給顧問|轉接真人|由顧問',text):failures.append(check)
                        if check=='question_last' and re.search(r'[？?]',text) and not re.search(r'[？?][～~\s]*$',text):failures.append(check)
                        if check=='no_extra_opening':
                            authored = bundle['reply'].get('opening_messages') or [bundle['reply'].get('opening_message','')]
                            other = [m.get('content','') for m in emitted if m.get('content') not in authored]
                            if any(re.match(r'\s*(?:您好|你好|哈囉|我是)',t) for t in other):failures.append(check)
                        if check=='contact_by_now':
                            sent = '\n'.join(m.get('content','') for m in row.messages
                                             if m.get('status')=='simulated_delivered' and m.get('direction')=='outgoing')
                            if not re.search(r'(?:LINE\s*QR\s*Code|信箱|Email).*?[？?]',sent,re.I):failures.append(check)
                    for token in step.get('contains',[]):
                        if token not in text:failures.append('missing:'+token)
                    for token in step.get('excludes',[]):
                        if token in text:failures.append('unexpected:'+token)
                    record={'id':step['id'],'request':step,'now':row.virtual_now,'emitted':emitted,
                            'text':text,'state':state,'profile':row.memory,'failures':failures}
                    records.append(record)
                    print(json.dumps({'case':case['id'],'step':step['id'],'text':text[:550],'failures':failures},ensure_ascii=False),flush=True)
            except Exception as exc:
                records.append({'id':'execution_error','failures':[str(exc)]})
            result={'case':case,'steps':records,'messages':row.messages,'profile':row.memory,'state':service.state(row),
                    'runs':[{'id':r.id,'status':r.status,'error':r.error_code,'input':r.input_snapshot,
                             'decision':r.decision,'trace':r.trace} for r in db.scalars(select(AutomationRun).order_by(AutomationRun.id))]}
            (args.output/(case['id']+'.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
        engine.dispose()
        # Close this test thread's transport before its loop goes away.
        from app.model_transport import _transport
        runner = getattr(_transport, 'runner', None)
        if runner:
            client = getattr(_transport, 'client', None)
            if client:
                runner.run(client.aclose())
            runner.run(runner.get_loop().shutdown_asyncgens())
            runner.close()
            for key in ('runner','client','identity'):
                if hasattr(_transport,key):delattr(_transport,key)
        return {'case':case['id'],'steps':len(records),'failures':[{ 'step':r['id'],'errors':r['failures']} for r in records if r['failures']]}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results=list(pool.map(run_case,cases))
    (args.output/'summary.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(results,ensure_ascii=False))


if __name__ == '__main__':
    main()
