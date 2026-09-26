"""Read-only real-model acceptance replay. No database or delivery workers.

Only the configured model endpoint may receive network requests. The route
material ids simulate availability; physical media delivery is integration-tested
separately. Results are checkpoints, not a claim of universal model correctness.
"""
from __future__ import annotations

import argparse
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
from app.route_packages import ROUTES
from app.reception_v2 import ENGINE_RELEASE_ID
from app.reception_v2.runtime import PROMPT_VERSION
from app.route_reply import (make_route_snapshot, ROUTE_SNAPSHOTS_KEY, update_content_progress_values,
                            prepare_route_reply_values, journey_context_from_values)


NINE = 'peach_9d_2027'
ELEVEN = 'peach_11d_2027'
CASES = [
    ('full_intro_9', '把桃花9日完整介绍发我，行程图、住宿和车的资料都要', NINE, 'full_intro'),
    ('full_intro_11', '请完整介绍11日珠峰路线，包括行程图、酒店和车', ELEVEN, 'full_intro'),
    ('itinerary_11', '我要去珠峰的行程', '', 'itinerary'),
    ('switch_9_to_11', '改成有珠峰的，給我行程', NINE, 'itinerary'),
    ('itinerary_9', '想看桃花9日的完整行程', '', 'itinerary'),
    ('rail_in', '可以搭青鐵入藏嗎', NINE, 'arrival'),
    ('rail_out', '回程可以搭青藏鐵路嗎？票有包含嗎？', ELEVEN, 'arrival'),
    ('meeting', '這個行程在哪裡集合', NINE, 'meeting'),
    ('chengdu', '所以在成都集合嗎？', ELEVEN, 'meeting'),
    ('permit', '入藏函要在哪裡拿？', NINE, 'permit'),
    ('other_transfer', '我不經成都，從重慶飛呢？入藏函怎麼拿？', ELEVEN, 'special'),
    ('age_65', '我們是台灣人，65歲能參加嗎？', ELEVEN, 'age'),
    ('age_75', '台灣旅客剛滿75歲可以報名嗎？', NINE, 'age'),
    ('age_76', '我爸爸台灣人76歲，還能參加嗎？', ELEVEN, 'age_over'),
    ('age_unspecified', '70歲可以參加嗎？', ELEVEN, 'age'),
    ('age_other', '我是香港人70歲，也一定要健康證明嗎？', NINE, 'special'),
    ('health', '有心臟病，交健康證明就保證可以去珠峰嗎？', ELEVEN, 'health'),
    ('contact_then_question', '先不留LINE了，在哪集合？', NINE, 'meeting'),
    ('comparison', '9日和11日都有什麼差別？', NINE, 'comparison'),
    ('repeat_itinerary', '剛剛行程圖沒收到，珠峰那份請再發一次', ELEVEN, 'resend'),
    ('age_64', '台灣旅客64歲也能報名嗎，還是最低65歲？', NINE, 'age'),
    ('age_range', '台灣人65到75歲有什麼參加條件？', ELEVEN, 'age'),
    ('switch_11_to_9', '不要珠峰了，給我9日行程', ELEVEN, 'itinerary'),
    ('date_not_duration', '我們3月9日出發，在哪集合？', ELEVEN, 'meeting'),
    ('silence_after_itinerary', '', NINE, 'silence_reply'),
    ('silence_before_itinerary_receipt', '', NINE, 'silence_skip'),
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--case', action='append')
    parser.add_argument('--journey', action='store_true', help='Replay a continuous customer conversation and silence event')
    args = parser.parse_args()
    rows = []
    original_send = httpx.Client.send
    original_async_send = httpx.AsyncClient.send
    allowed = settings.deepseek_base_url.rstrip('/') + '/chat/completions'

    def guarded_send(client, request, *a, **kw):
        if str(request.url) != allowed or request.method != 'POST':
            raise RuntimeError('evaluation_network_destination_blocked')
        return original_send(client, request, *a, **kw)

    async def guarded_async_send(client, request, *a, **kw):
        if str(request.url) != allowed or request.method != 'POST':
            raise RuntimeError('evaluation_network_destination_blocked')
        return await original_async_send(client, request, *a, **kw)

    materials = [{'key': key} for route in ROUTES.values() for group in route['groups'].values() for key in group['assets']]
    if args.journey:
        from app.db import SessionLocal
        from app.material_library import candidate_materials
        with SessionLocal() as db:
            materials = candidate_materials(db, 1)
    sequence = CASES if not args.journey else [
        ('new_9', '給我看桃花9日行程', '', 'itinerary'),
        ('ask_rail', '可以搭青鐵入藏嗎', '', 'arrival'),
        ('ask_meeting', '那在哪集合呢？', '', 'meeting'),
        ('switch_11', '我們改去珠峰，給我那條行程', '', 'itinerary'),
        ('ask_age', '我媽媽是台灣人，75歲可以參加嗎？', '', 'age'),
        ('pause', '我先跟家人討論', '', 'pause'),
        ('silence_after_pause', '', '', 'silence'),
    ]
    journey, history = {}, []
    with patch.object(httpx.Client, 'send', guarded_send), patch.object(httpx.AsyncClient, 'send', guarded_async_send):
        for key, customer, route, category in sequence:
            if args.case and key not in args.case:
                continue
            context = {'module': 'reply', 'engine_version': 'v2', 'customer_text': customer,
                       'route_variant': route, 'context_messages': [], 'available_materials': materials,
                       'journey': {'route_variant': route, 'stage': 'value_building'}}
            if route:
                context['journey']['slots'] = {ROUTE_SNAPSHOTS_KEY: {route: make_route_snapshot(route, ROUTES[route])}}
            if args.journey:
                context.update(journey=journey, route_variant=journey.get('route_variant', ''), context_messages=history)
                if category == 'silence':
                    context['module'] = 'silence_touch'
            if key == 'contact_then_question':
                context['journey']['stage'] = 'contact_requested'
                context['lead_capture'] = {'status': 'asked'}
                context['context_messages'] = [{'role': 'assistant', 'content': '您方便留一個聯絡方式嗎？'}]
            if key == 'repeat_itinerary':
                slots, groups, _ = update_content_progress_values(route, context['journey']['slots'], [],
                    'itinerary_overview', asset_keys=['routes12-11d-itinerary'], text_delivered=True)
                context['journey']['slots'] = slots
                context['journey']['sent_content_groups'] = groups
                context['journey']['sent_asset_keys'] = ['routes12-11d-itinerary']
            if category in {'silence_reply', 'silence_skip'}:
                context['module'] = 'silence_touch'
                context['context_messages'] = [{'role': 'customer', 'content': '想看9日行程'},
                                               {'role': 'assistant', 'content': '這是9日行程總覽。'}]
                if category == 'silence_reply':
                    slots, groups, _ = update_content_progress_values(route, context['journey']['slots'], [],
                        'itinerary_overview', asset_keys=['routes12-9d-itinerary'], text_delivered=True)
                    context['journey'] = journey_context_from_values(route, stage='value_building', slots=slots, sent_groups=groups)
            start = time.monotonic()
            row = {'key': key, 'input': customer, 'category': category}
            try:
                decision, logs, _, trace = generate_decision(context)
                row.update(decision=asdict(decision), flow=trace.get('flow'), calls=len(logs),
                           verified=trace.get('fact_verification_passed'))
                issues = []
                if decision.evidence_refs and trace.get('fact_verification_passed') is not True:
                    issues.append('fact_verification_not_passed')
                if category in {'meeting', 'permit', 'age', 'age_over', 'arrival'} and decision.journey_stage == 'considering':
                    issues.append('unsupported_considering_state')
                if category in {'itinerary', 'resend'}:
                    expected = 'routes12-9d-itinerary' if key in {'itinerary_9', 'new_9', 'switch_11_to_9'} else 'routes12-11d-itinerary'
                    if decision.material_keys != [expected]: issues.append('missing_or_wrong_itinerary')
                if category == 'resend' and not decision.allow_material_resend: issues.append('resend_not_enabled')
                if category == 'full_intro':
                    groups = [item['group_key'] for item in decision.v2_delivery_sections]
                    if groups[:3] != ['brand_positioning', 'itinerary_overview', 'hotel_reference']:
                        issues.append('incomplete_introduction_order')
                    if 'vehicle_reference' not in groups:
                        issues.append('missing_vehicle_section')
                    if route == ELEVEN and ('rongbuk_reference' not in groups or groups.index('rongbuk_reference') < groups.index('hotel_reference')):
                        issues.append('missing_or_misordered_rongbuk')
                if category in {'meeting', 'permit', 'age', 'age_over', 'arrival'} and decision.material_keys:
                    issues.append('unrequested_materials')
                if category == 'meeting' and any(term in (decision.reply or '') for term in ('嘎拉', '波密', '第八天', 'D1')):
                    issues.append('unrequested_itinerary_expansion')
                if category not in {'silence', 'silence_skip'} and not decision.reply: issues.append('missing_answer')
                if category == 'silence' and decision.action != 'no_action': issues.append('disturbed_considering_customer')
                if category == 'silence_skip' and decision.action != 'no_action': issues.append('photo_before_itinerary_receipt')
                if category == 'silence_reply' and decision.action != 'reply': issues.append('missing_useful_followup')
                if args.journey:
                    decision, progress = prepare_route_reply_values(decision,
                        current_route=journey.get('route_variant', ''), stage=journey.get('stage', 'route_selection'),
                        slots=journey.get('slots'), sent_groups=journey.get('sent_content_groups'))
                    if decision.content_group_key and decision.action == 'reply':
                        progress['slots'], progress['sent_content_groups'], _ = update_content_progress_values(
                            decision.route_variant, progress['slots'], progress['sent_content_groups'], decision.content_group_key,
                            asset_keys=decision.material_keys, delivered_text=decision.reply)
                    journey = journey_context_from_values(decision.route_variant, stage=progress['stage'],
                        slots=progress['slots'], sent_groups=progress['sent_content_groups'])
                    row['simulated_journey_stage'] = journey['stage']
                    row['simulated_journey_route'] = journey['route_variant']
                    history = [*history, {'role': 'customer', 'content': customer}, {'role': 'assistant', 'content': decision.reply or ''}]
                row['structural_issues'] = issues
            except Exception as exc:
                row['error'] = str(exc)[:200]
                row['error_logs'] = getattr(exc, 'logs', [])
            row['elapsed_ms'] = round((time.monotonic() - start) * 1000)
            rows.append(row)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps({'outbound': False, 'network_allowlist': 'configured model endpoint only',
                'engine_release_id': ENGINE_RELEASE_ID, 'prompt_version': PROMPT_VERSION,
                'model': settings.deepseek_model, 'cases': rows}, ensure_ascii=False, indent=2), encoding='utf-8')
            print(key, row.get('error', row.get('structural_issues')), flush=True)
    if not rows or any(row.get('error') or row.get('structural_issues') for row in rows):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
