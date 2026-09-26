"""Real-model replay of business feedback; only model HTTP is allowed."""
import argparse
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import sys
import time
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import settings
from app.decision_service import generate_decision
from app.reception_v2 import ENGINE_RELEASE_ID
from app.reception_v2.route_profiles import route_profile
from app.route_packages import ROUTES
from app.route_reply import bind_new_route_snapshot, update_content_progress_values, journey_context_from_values


def packet(route):
    slots = bind_new_route_snapshot(route, {'party_size': 4})
    sent = []
    for key, group in ROUTES[route]['groups'].items():
        if group.get('initial_delivery'):
            slots, sent, _ = update_content_progress_values(route, slots, sent, key,
                delivered_text=group['text'], asset_keys=group['assets'])
    return {'engine_version': 'v2', 'module': 'reply', 'route_variant': route,
        'journey': journey_context_from_values(route, 'value_building', slots, sent),
        'available_materials': [{'key': key} for g in ROUTES[route]['groups'].values() for key in g['assets']],
        'context_messages': [
            {'direction': 'incoming', 'content': '請把完整行程、住宿和用車照片傳給我'},
            {'direction': 'outgoing', 'content': '這是行程圖、住宿和用車照片。這次幾位同行呢？'}],
        'source_message_id': 'offline-business-replay'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--case', action='append')
    args = parser.parse_args()
    original = httpx.Client.send
    original_async = httpx.AsyncClient.send
    def guard(client, request, *a, **kw):
        assert request.method == 'POST' and str(request.url) == settings.deepseek_base_url.rstrip('/') + '/chat/completions'
        return original(client, request, *a, **kw)
    async def async_guard(client, request, *a, **kw):
        assert request.method == 'POST' and str(request.url) == settings.deepseek_base_url.rstrip('/') + '/chat/completions'
        return await original_async(client, request, *a, **kw)
    rows = []
    with patch.object(httpx.Client, 'send', guard), patch.object(httpx.AsyncClient, 'send', async_guard):
        for route in ('peach_9d_2027', 'peach_11d_2027'):
            for key, text in [('party', '4'), ('correction', '改成6位喔'),
                ('minimum', '這個幾人成行'), ('smallgroup', '小團一般幾個人？'),
                ('party_price', '我們4位，多少錢？'), ('resend', '行程圖没收到，请再发一次'),
                ('useful_silence', ''), ('exhausted', ''), ('considering', ''), ('optout', '')]:
                case = route + ':' + key
                if args.case and case not in args.case:
                    continue
                context = packet(route)
                context['customer_text'] = text
                if not text:
                    context['module'] = 'silence_touch'
                    context['context_messages'].append({'direction': 'incoming', 'content': '謝謝'})
                if key == 'exhausted':
                    context['journey']['slots']['_v2_state'] = {
                        'provided_fact_ids': {route: route_profile(route)['followup_candidates']}}
                if key == 'considering':
                    context['journey']['stage'] = 'considering'
                if key == 'optout':
                    context['journey']['slots']['_v2_state'] = {'proactive_opt_out': True}
                start = time.monotonic()
                row = {'case': case, 'input': text}
                try:
                    decision, logs, _, trace = generate_decision(deepcopy(context))
                    row.update(decision=asdict(decision), trace=trace, calls=len(logs))
                    body = decision.reply or ''
                    problems = []
                    if key in {'exhausted', 'considering', 'optout'}:
                        if decision.action != 'no_action' or logs: problems.append('ineligible_followup_called_model')
                    elif key == 'useful_silence':
                        if decision.action != 'reply' or not decision.evidence_refs: problems.append('missing_new_value')
                        if any(term in body for term in ('整理完整', '完整行程再', '留個', '留一個')): problems.append('repeated_material_or_contact_offer')
                    else:
                        if not body: problems.append('missing_reply')
                        if key in {'party', 'correction'}:
                            if decision.material_keys or decision.follow_up_question or len(body) > 80: problems.append('profile_update_scope')
                            if any(term in body for term in ('住宿', '用車', '小團', '行程傳', '人民幣')): problems.append('profile_update_resells')
                        if key == 'resend':
                            if decision.material_keys != ROUTES[route]['groups']['itinerary_overview']['assets'] or not decision.allow_material_resend:
                                problems.append('resend_not_delivered')
                        if key == 'minimum':
                            if any(term in body for term in ('保證出團', '4人成行', '4人成團', '以合約', '以合同')) or decision.follow_up_question:
                                problems.append('unsupported_or_irrelevant_formation_answer')
                            if not trace.get('confirmation_questions') or decision.action != 'handoff': problems.append('missing_targeted_confirmation_task')
                        if key == 'smallgroup' and (trace.get('confirmation_questions') or decision.action != 'reply'): problems.append('unnecessary_group_size_handoff')
                    row['issues'] = problems
                except Exception as exc:
                    row['error'] = str(exc)
                    row['logs'] = getattr(exc, 'logs', [])
                row['seconds'] = round(time.monotonic() - start, 2)
                rows.append(row)
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps({'release': ENGINE_RELEASE_ID, 'customer_messages_sent': 0,
                    'cases': rows}, ensure_ascii=False, indent=2), encoding='utf8')
                print(case, row.get('error', row.get('issues')), flush=True)
    if any(row.get('error') or row.get('issues') for row in rows):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
