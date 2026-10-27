"""Run linked reception journeys in memory; only the model endpoint may be called.

Uses a read-only production configuration/media snapshot. Media resolution is
simulated, but intake, buffering, delivery order, timing and decisions use V3.
"""
import argparse
import hashlib
from copy import deepcopy
import json
import logging
import os
from pathlib import Path
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar


def has_unrequested_price(text, bundle):
    """Check every configured route, rather than only the original peach prices."""
    amounts = {12180, 12780, 10780}
    for route in bundle.get('routes', {}).values():
        amounts.update(b['price_cny'] for b in (route.get('seasonal_pricing') or {}).get('bands', []))
        for script in route.get('scripts', []):
            if script['id'] == 'price' or script['id'].startswith('price_'):
                amounts.update(int(m.replace(',', '')) for m in re.findall(
                    r'(?:¥|￥|人民幣|人民币|RMB|CNY)\s*([\d,]+)', script['answer_text']))
    if any(re.search(r'(?<!\d)' + ',?'.join(str(a)) + r'(?!\d)', text) for a in amounts):
        return True
    return bool(re.search(r'單房差.*\d|加\s*400', text))


def has_altitude_guarantee(text):
    for clause in re.split(r'[，,。！!；;\n]',text):
        for match in re.finditer(r'不[會会]高反|保[證证]安全',clause):
            if not re.search(r'(?:不(?:能)?|無法|无法|沒辦法|没办法).{0,4}保[證证]',clause[:match.start()]):
                return True
    return False


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser()
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--cases', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--ledger', type=Path, required=True)
    parser.add_argument('--image-fixtures', type=Path, help='Isolated URL-to-local-image mapping; images still go to the real vision model')
    parser.add_argument('--workers', type=int, choices=(1, 2), default=2)
    parser.add_argument('--full-journeys', action='store_true', help='Require continuous entry-to-followup conversations, without seeded history')
    parser.add_argument('--check-only', action='store_true', help='Validate test data without loading settings or calling a model')
    args = parser.parse_args()
    args.output = args.output.resolve()
    if args.image_fixtures:
        args.image_fixtures = args.image_fixtures.resolve()
    snapshot = json.loads(args.snapshot.read_text(encoding='utf-8-sig'))
    cases = json.loads(args.cases.read_text(encoding='utf-8-sig'))
    if args.full_journeys:
        for case in cases:
            if case.get('replay_context') or len([s for s in case['steps'] if s.get('text')]) < 4:
                raise ValueError('full_journey_requires_entry_and_four_customer_replies:' + case['id'])
            if not any(s.get('next_check') for s in case['steps']):
                raise ValueError('full_journey_requires_silence_and_resume:' + case['id'])
            silence = next(i for i,s in enumerate(case['steps']) if s.get('next_check'))
            if not any(s.get('text') for s in case['steps'][silence+1:]):
                raise ValueError('full_journey_requires_reply_after_silence:' + case['id'])
    if args.check_only:
        print(json.dumps({'cases':len(cases), 'customer_turns':sum(1+sum(bool(s.get('text')) for s in c['steps']) for c in cases),
                          'real_model_calls':0, 'full_journeys':args.full_journeys}, ensure_ascii=False))
        return
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output/'tested-snapshot.json').write_text(json.dumps(snapshot, ensure_ascii=False, indent=2),encoding='utf8')
    (args.output/'tested-cases.json').write_text(json.dumps(cases, ensure_ascii=False, indent=2),encoding='utf8')
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
    image_sources = []
    if args.image_fixtures:
        import base64
        from app.reception_v3 import customer_images
        mapping = json.loads(args.image_fixtures.read_text(encoding='utf8'))
        encoded = {}
        for url, filename in mapping.items():
            path = Path(filename).resolve()
            if not customer_images.image_url({'file_type':'image','data_url':url}):
                raise ValueError('untrusted_fixture_url')
            data = path.read_bytes()
            if len(data) > customer_images.MAX_BYTES:
                raise ValueError('image_fixture_too_large')
            from PIL import Image
            with Image.open(path) as im:
                mime = Image.MIME[im.format]
                im.verify()
            encoded[url] = 'data:'+mime+';base64,'+base64.b64encode(data).decode()
            image_sources.append(path)
        def fixture_image(url, deadline):
            if url not in encoded:
                raise ValueError('image_fixture_missing')
            return encoded[url]
        customer_images.download_image = fixture_image
        settings.customer_image_vision_enabled = True
    source_paths = [Path(__file__), backend/'app/reception_v3/prompt.py',
                    backend/'app/reception_v3/runtime.py', backend/'app/reception_v3/service.py',
                    backend/'app/reception_v3/live.py', *sorted((backend/'app/reception_v3/skills').glob('*/SKILL.md'))]
    source_paths += [backend/'app/reception_v3/quotes.py', backend/'app/reception_v3/skills.py', backend/'app/reception_config.py']
    source_paths += [backend/'app/config.py', backend/'app/reception_v3/repetition.py']
    source_paths += [backend/'app/reception_v3/seasonal_quotes.py', backend/'app/reception_v3/party_quotes.py']
    source_paths += [backend/'app/reception_v3/large_party.py']
    source_paths += [backend/'app/reception_v3/route_choice.py', backend/'app/reception_v3/customer_reactions.py',
                     backend/'app/reception_v3/followup_window.py', backend/'app/reception_v3/material_rules.py',
                     backend/'app/reception_v3/customer_images.py']
    def fingerprints():
        return {str(p.relative_to(backend.parent) if p.is_relative_to(backend.parent) else p): hashlib.sha256(p.read_bytes()).hexdigest() for p in source_paths + image_sources}
    tested_sources = fingerprints()
    (args.output/'manifest.json').write_text(json.dumps({'sources': tested_sources,
        'snapshot_sha256': hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
        'cases_sha256': hashlib.sha256(json.dumps(cases, sort_keys=True, ensure_ascii=False).encode()).hexdigest()}, indent=2), encoding='utf8')
    active_bundle = ContextVar('journey_configuration')
    service.compile_skills = lambda db: deepcopy(active_bundle.get())
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
        bundle = deepcopy(snapshot['bundle'])
        # Scenario overrides affect only this isolated in-memory conversation.
        if case.get('silence'):
            from app.reception_config import SilenceSettings
            bundle['silence'] = SilenceSettings.model_validate({**bundle['silence'], **case['silence']}).model_dump()
        active_bundle.set(bundle)
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
            if case.get('entry_attachments'):
                from app.reception_v3.customer_images import attachment_metadata
                row.messages = [{**m, 'content_type':'image', 'attachments':attachment_metadata(case['entry_attachments'])} for m in row.messages]
            for index, text in enumerate(case.get('entry_messages', [])):
                service.add_message(db,row,text,f'entry-extra-{index}')
            row.controls = {**row.controls, 'evaluation_labels':case.get('labels',[])}
            db.commit()
            if case.get('replay_context'):
                # Clone a read-only historical input into this isolated session.
                seed = deepcopy(case['replay_context'])
                row.virtual_now = seed['now']
                row.messages = seed['messages']
                row.memory = seed['profile']
                row.controls = {**row.controls, 'route_variant': seed['route_variant']}
                service.save(row, {**seed['state'], 'pending_event': seed['event']})
                db.commit()
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
                for i, step in enumerate(([] if case.get('replay_context') else [entry]) + case['steps']):
                    before = {m['id'] for m in row.messages if m.get('status') == 'simulated_delivered'}
                    previous_drafts = {m['id'] for m in row.messages if m.get('status') == 'draft'}
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
                        if check=='intake':
                            intake = bundle['routes'].get(row.controls.get('route_variant',''),{}).get('intake') or bundle['intake']
                            if not emitted or emitted[0]['content']!=intake['question']:failures.append(check)
                        if check=='no_send' and emitted:failures.append(check)
                        if check=='no_duplicate_messages':
                            texts=[m.get('content','').strip() for m in emitted if m.get('content')]
                            if len(texts)!=len(set(texts)):failures.append(check)
                        if check=='no_contact' and re.search(r'LINE|QR\s*Code|Email|信箱',text,re.I):failures.append(check)
                        if check=='no_price' and has_unrequested_price(text, bundle):failures.append(check)
                        if check=='no_altitude_guarantee' and has_altitude_guarantee(text):failures.append(check)
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
                        if check in ('one_itinerary_map', 'no_itinerary_map'):
                            from app.reception_v3.material_rules import itinerary_previews
                            previews = itinerary_previews(bundle,snapshot['materials'],[])
                            expected = step.get('itinerary_route')
                            keys = {a['key'] for route,p in previews.items() if not expected or route==expected
                                    for a in p['assets']}
                            maps = [m for m in emitted if m.get('content_type')=='image' and m.get('asset_key') in keys]
                            if ((check=='one_itinerary_map' and (len(maps)!=1 or not maps[0].get('media_id') or not maps[0].get('media_hash')))
                                    or (check=='no_itinerary_map' and maps)):
                                failures.append(check)
                        if check=='text_only' and any(m.get('content_type') != 'text' for m in emitted):failures.append(check)
                        if check=='no_third_person' and re.search(r'顧問可以|請顧問|讓顧問|轉人工|轉給顧問|轉接真人|由顧問',text):failures.append(check)
                        if check=='exact_quote':
                            from app.reception_v3.quotes import personalize_quotes
                            route = personalize_quotes(db, row, bundle)['routes'][row.controls['route_variant']]
                            quote = next(s['answer_text'] for s in route['scripts'] if s['id']=='price')
                            if text.count(quote) != 1:failures.append(check)
                        if check=='no_surcharge' and re.search(r'單房差|独立成团.*400|獨立成團.*400|加價',text):failures.append(check)
                        if check=='only_requested_surcharge':
                            amounts = re.findall(r'([0-9][0-9,]*)\s*(?:元|/人)',text)
                            if not amounts or any(a.replace(',','') != '400' for a in amounts):failures.append(check)
                        if check=='question_last' and re.search(r'[？?]',text) and not re.search(r'[？?][～~\s]*$',text):failures.append(check)
                        if check=='one_question_required_last' and (len(re.findall(r'[？?]',text)) != 1 or not re.search(r'[？?][～~\s]*$',text)):failures.append(check)
                        if check=='no_unsolicited_exclusions' and re.search(r'不(?:包含|包括|含)|另[付收]|自[付費费]',text):failures.append(check)
                        if check=='no_extra_opening':
                            authored = bundle['reply'].get('opening_messages') or [bundle['reply'].get('opening_message','')]
                            other = [m.get('content','') for m in emitted if m.get('content') not in authored]
                            if any(re.match(r'\s*(?:您好|你好|哈囉|我是)',t) for t in other):failures.append(check)
                        if check=='no_reask_party' and re.search(r'幾位|幾個人|多少人|幾人',text):failures.append(check)
                        if check=='no_reask_month' and re.search(r'幾月|什麼時候出發|什么时候出发',text):failures.append(check)
                        if check=='no_new_introduction' and any(m.get('v3_kind')=='introduction' for m in emitted):failures.append(check)
                        if check=='previous_drafts_cancelled' and any(m['id'] in previous_drafts and m.get('status')!='cancelled' for m in row.messages):failures.append(check)
                        if check=='no_rejected_material':
                            from app.reception_v3.route_choice import rejected_asset
                            if any(rejected_asset(m.get('asset_key'),snapshot['materials'],row.memory) for m in emitted if m.get('asset_key')):
                                failures.append(check)
                        if check=='contact_by_now':
                            sent = '\n'.join(m.get('content','') for m in row.messages
                                             if m.get('status')=='simulated_delivered' and m.get('direction')=='outgoing')
                            if not re.search(r'(?:LINE\s*QR\s*Code|信箱|Email).*?[？?]',sent,re.I):failures.append(check)
                    for token in step.get('contains',[]):
                        if token not in text:failures.append('missing:'+token)
                    for alternatives in step.get('contains_any',[]):
                        if not any(token in text for token in alternatives):
                            failures.append('missing_any:'+str(alternatives))
                    for token in step.get('excludes',[]):
                        if token in text:failures.append('unexpected:'+token)
                    if 'expected_route' in step and row.controls.get('route_variant','') != step['expected_route']:
                        failures.append('unexpected_route:'+row.controls.get('route_variant',''))
                    for key, expected in step.get('profile_equals',{}).items():
                        if row.memory.get(key) != expected:
                            failures.append('profile_mismatch:'+key+':'+str(row.memory.get(key)))
                    for key in step.get('profile_absent',[]):
                        if row.memory.get(key) is not None:failures.append('unexpected_profile:'+key)
                    if step.get('offer_status') and (row.memory.get('_route_offer') or {}).get('status') != step['offer_status']:
                        failures.append('unexpected_offer_status:'+str(row.memory.get('_route_offer')))
                    for key in step.get('state_present', []):
                        if not state.get(key):failures.append('missing_state:'+key)
                    if 'image_count' in step and sum(m.get('content_type')=='image' for m in emitted) != step['image_count']:
                        failures.append('unexpected_image_count')
                    for route in step.get('excluded_asset_routes',[]):
                        if any(m.get('content_type')!='text' and materials.get(m.get('asset_key'),{}).get('routes')==[route] for m in emitted):
                            failures.append('unexpected_asset_route:'+route)
                    record={'id':step['id'],'request':step,'now':row.virtual_now,'emitted':emitted,
                            'text':text,'route':row.controls.get('route_variant'),'state':state,'profile':row.memory,'failures':failures}
                    records.append(deepcopy(record))
                    print(json.dumps({'case':case['id'],'step':step['id'],'text':text[:550],'failures':failures},ensure_ascii=False),flush=True)
            except Exception as exc:
                records.append({'id':'execution_error','failures':[str(exc)]})
            result={'case':case,'steps':records,'messages':row.messages,'profile':row.memory,'state':service.state(row),
                    'runs':[{'id':r.id,'status':r.status,'error':r.error_code,'input':r.input_snapshot,
                             'decision':r.decision,'trace':r.trace} for r in db.scalars(select(AutomationRun).order_by(AutomationRun.id))]}
            model_calls=sum(r['trace'].get('model_http_request_count',0) for r in result['runs'])
            if args.full_journeys and not any(r['id']=='execution_error' for r in records):
                missing=[]
                if not model_calls:missing.append('full_journey_has_no_real_model_calls')
                if not any(r['input'].get('event')=='silence_due' for r in result['runs']):
                    missing.append('silence_event_was_not_executed')
                if missing:records.append({'id':'journey_integrity','failures':missing})
            result['real_model_calls']=model_calls
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
        return {'case':case['id'],'steps':len(records),'real_model_calls':model_calls,
                'failures':[{ 'step':r['id'],'errors':r['failures']} for r in records if r['failures']]}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results=list(pool.map(run_case,cases))
    if fingerprints() != tested_sources:
        raise RuntimeError('effect_test_sources_changed_during_run')
    (args.output/'summary.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(results,ensure_ascii=False))
    if any(case['failures'] for case in results):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
