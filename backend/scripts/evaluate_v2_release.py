"""Isolated model-only acceptance; no database writes, channels or workers."""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
from queue import Queue, Empty
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import statistics
import sys
import time
from unittest.mock import patch
import httpx


def write_atomic_json(target, payload):
    """Keep complete evidence when Windows readers briefly hold the target."""
    temporary=target.with_suffix(target.suffix+'.tmp')
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    for attempt in range(8):
        try:
            temporary.replace(target)
            return
        except PermissionError as exc:
            if getattr(exc,'winerror',None) not in {5,32,33} or attempt==7:
                raise
            time.sleep(min(.025 * 2**attempt, .2))

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import settings
from app.decision_service import generate_decision
from app.reception_v2 import ENGINE_RELEASE_ID
from app.route_packages import ROUTES
from app.reception_config import default_reception_configuration, policy_from_configuration
from app.route_reply import make_route_snapshot, ROUTE_SNAPSHOTS_KEY
from app.release_provenance import source_fingerprint, assert_source_unchanged
from v2_acceptance_cases import cases


def evaluate(case, repeat, engine):
    route = case['route']
    context = {'module': 'reply', 'engine_version': engine, 'customer_text': case['customer'],
        'reception_policy': policy_from_configuration(default_reception_configuration()),
        'source_message_id': f"{case['id']}:{repeat}", 'context_complete': True,
        'now': '2026-09-20T02:00:00+00:00', 'last_customer_at': '2026-09-20T02:00:00+00:00',
        'context_messages': [], 'route_variant': route,
        'available_materials': [{'key': key, 'what_it_shows':g['text']} for g in ROUTES[route]['groups'].values() for key in g['assets'] if key != 'china2go-altitude-guide-v1'],
        'journey': {'route_variant': route, 'stage': 'value_building',
                    'slots': {ROUTE_SNAPSHOTS_KEY: {route: make_route_snapshot(route, ROUTES[route])}}}}
    row = {**case, 'repeat': repeat, 'engine': engine, 'issues': []}
    start = time.monotonic()
    try:
        decision, logs, digest, trace = generate_decision(context)
        row.update(decision=asdict(decision), trace=trace, prompt_digest=digest,
            calls=[{k: log.get(k) for k in ('node','duration_ms','status','input_tokens','output_tokens','error_code','response_meta','claim_checks','scope_check','unsupported_claims','contract_violations','rejected_reply')} for log in logs])
        body = '\n'.join(s['text'] for s in decision.v2_delivery_sections) if decision.v2_delivery_sections else decision.reply or ''
        row['rendered_text'] = body
        for expression in case['required']:
            if not re.search(expression, body): row['issues'].append('missing_required:' + expression)
        if not body and case['category'] != 'optout': row['issues'].append('no_customer_answer')
        if '比較' in body or '比较' in body: row['issues'].append('prohibited_copy')
        category = case['category']
        if case.get('expected_action') and decision.action != case['expected_action']:
            row['issues'].append('unexpected_action:'+decision.action)
        if category == 'handoff' and decision.action != 'handoff': row['issues'].append('human_task_missing')
        if category not in {'pause','optout','handoff'} and decision.journey_stage == 'considering': row['issues'].append('false_considering')
        if category == 'pause' and (decision.journey_stage != 'considering' or decision.lead_action == 'ask' or len(body) > 70):
            row['issues'].append('pause_not_respected')
        if category == 'refusal' and (decision.action == 'handoff' or decision.lead_action == 'ask'):
            row['issues'].append('channel_refusal_misunderstood')
        if engine == 'v2':
            if case['id'].startswith('undecided'):
                if decision.material_keys or decision.v2_delivery_sections or any(e.get('type')=='material_requested' for e in decision.v2_events):
                    row['issues'].append('permission_question_not_material_request')
                if decision.route_variant != route: row['issues'].append('bound_route_lost')
                if not decision.slots.get('departure_window'): row['issues'].append('uncertain_departure_not_stored')
                evidence=decision.slot_evidence.get('departure_window','')
                if not evidence or evidence not in case['customer']: row['issues'].append('departure_current_evidence_missing')
                if re.search(r'(?:9|九).*(?:11|十一).*(?:哪|選|选)|(?:哪|選|选).*(?:9|九).*(?:11|十一)',body):
                    row['issues'].append('known_route_reasked')
            if category == 'optout' and not any(e['type'] == 'contact_refused' and e.get('scope') == 'all' for e in decision.v2_events):
                row['issues'].append('optout_event_missing')
            if category in {'intro','itinerary'}:
                image = 'routes12-11d-itinerary' if route.endswith('11d_2027') else 'routes12-9d-itinerary'
                if image not in decision.material_keys: row['issues'].append('requested_itinerary_missing')
            if category == 'intro':
                sections = decision.v2_delivery_sections
                groups = [s['group_key'] for s in sections if not s.get('answers_customer_question')]
                expected = ['brand_positioning','itinerary_overview','hotel_reference']
                if route == 'peach_11d_2027': expected += ['rongbuk_reference']
                expected += ['vehicle_reference']
                if groups[:len(expected)] != expected: row['issues'].append('intro_incomplete_or_wrong_order')
                if case['id'].startswith('fullintro') and (not sections or sections[0]['group_key']!='brand_positioning'):
                    row['issues'].append('pure_introduction_must_start_with_brand')
                if case['id'].startswith('composite') and 'party_question' in groups: row['issues'].append('known_party_reasked')
            if decision.evidence_refs and trace.get('fact_verification_passed') is not True:
                row['issues'].append('unverified_facts')
        row['pass'] = not row['issues']
    except Exception as exc:
        row.update(error=str(exc)[:300], pass_=False)
        row['failure_logs'] = getattr(exc, 'attempts', getattr(exc, 'logs', []))
        row['pass'] = False
    row['elapsed_seconds'] = round(time.monotonic() - start, 3)
    return row


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--case', action='append')
    parser.add_argument('--repeat', type=int, default=1, help='0: final contract, critical 5 / other 3')
    parser.add_argument('--workers', type=int, default=2)
    parser.add_argument('--engine', choices=['v1','v2'], default='v2')
    args = parser.parse_args()
    selected = [c for c in cases() if not args.case or c['id'] in args.case]
    if not selected: raise SystemExit('no_cases_selected')
    jobs = [(c, i+1) for c in selected for i in range(args.repeat or (5 if c['critical'] else 3))]
    original, original_async = httpx.Client.send, httpx.AsyncClient.send
    endpoint = settings.deepseek_base_url.rstrip('/') + '/chat/completions'
    def send(client, request, *a, **kw):
        if str(request.url) != endpoint or request.method != 'POST': raise RuntimeError('network_destination_blocked')
        return original(client, request, *a, **kw)
    async def send_async(client, request, *a, **kw):
        if str(request.url) != endpoint or request.method != 'POST': raise RuntimeError('network_destination_blocked')
        return await original_async(client, request, *a, **kw)
    result = {'release': ENGINE_RELEASE_ID, 'model': settings.deepseek_model,
        'source_fingerprint':source_fingerprint(),
        'started_at': datetime.now(timezone.utc).isoformat(), 'engine': args.engine,
        'fixture_sha256': hashlib.sha256(Path(__file__).with_name('v2_acceptance_cases.py').read_bytes()).hexdigest(),
        'planned': len(jobs), 'workers': args.workers, 'real_customer_messages': 0, 'rows': []}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    rows_path=args.output.with_suffix('.rows.jsonl')
    progress_path=args.output.with_suffix('.progress.json')
    rows_path.write_text('',encoding='utf-8')
    def save(final=False):
        rows = result['rows']
        durations = sorted(r['elapsed_seconds'] for r in rows if r['category'] not in {'intro','pause','optout','handoff'})
        result['summary'] = {'completed': len(rows), 'passed': sum(r['pass'] for r in rows),
            'normal_samples': len(durations), 'p50': statistics.median(durations) if durations else None,
            'p95': durations[min(len(durations)-1, int(len(durations)*.95))] if durations else None}
        target=args.output if final else progress_path
        payload=result if final else {key:value for key,value in result.items() if key!='rows'}
        write_atomic_json(target,payload)
    with patch.object(httpx.Client, 'send', send), patch.object(httpx.AsyncClient, 'send', send_async):
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            pending, completed = Queue(), Queue()
            for job in jobs: pending.put(job)
            def worker():
                from app.deepseek_evaluation import close_deepseek_transport
                cold = True
                try:
                    while True:
                        try: case, repeat = pending.get_nowait()
                        except Empty: return
                        row = evaluate(case, repeat, args.engine)
                        row['transport_state'] = 'cold' if cold else 'reused'
                        cold = False
                        completed.put(row)
                finally:
                    # Production keeps a transport per worker. Close it on that
                    # same thread after all cases, retaining cold calls in stats.
                    close_deepseek_transport()
            futures = [pool.submit(worker) for _ in range(args.workers)]
            for _ in jobs:
                row = completed.get()
                result['rows'].append(row)
                with rows_path.open('a',encoding='utf-8') as stream:
                    stream.write(json.dumps(row,ensure_ascii=False)+'\n')
                save()
                print(f"{len(result['rows'])}/{len(jobs)} {row['id']} r{row['repeat']} pass={row['pass']} {row['elapsed_seconds']}s {row.get('issues') or row.get('error','')}", flush=True)
            for future in futures: future.result()
    result['finished_at'] = datetime.now(timezone.utc).isoformat()
    result['source_unchanged'] = source_fingerprint() == result['source_fingerprint']
    save(final=True)
    assert_source_unchanged(result['source_fingerprint'])
    raise SystemExit(0 if all(r['pass'] for r in result['rows']) else 1)


if __name__ == '__main__': main()
