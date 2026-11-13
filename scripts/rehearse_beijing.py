"""Real model replay on copied production code; no customer/network send tools."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'output/beijing-acceptance'
STAGE = OUT / 'stage'
ROUTE = 'beijing_micro_5d'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--only', default='')
    parser.add_argument('--output', default='model-results.json')
    args = parser.parse_args()
    os.environ.update(APP_PROFILE='evaluation', DATABASE_URL='sqlite:///:memory:',
                      OUTBOUND_MODE='disabled', CHATWOOT_WRITE_ENABLED='false', LIVE_SOP_ENABLED='false',
                      RELAY_BASE_URL='', RELAY_API_TOKEN='', CUSTOMER_IMAGE_VISION_ENABLED='false')
    os.chdir(STAGE / 'backend')
    sys.path.insert(0, str(STAGE / 'backend'))
    from app.reception_v3 import skills
    from app.reception_v3.runtime import run_agent
    config = json.loads((OUT / 'baseline.json').read_text(encoding='utf8'))['config']
    config['routing']['enabled_route_variants'].append(ROUTE)
    skills.get_reception_configuration = lambda db: deepcopy(config)
    bundle = skills.compile_skills(None)
    manifest = json.loads((ROOT / 'data/knowledge/china2go/route-releases/20261113-beijing/asset-manifest.json').read_text(encoding='utf8'))
    materials = [{'key': m['asset_key'], 'name': m['display_name'], 'routes': [ROUTE],
                  'media_hash': m['sha256'], 'content_group_key': m['content_group_key'],
                  'media_type': 'image', 'content_type': 'image', 'usage': m['display_name']} for m in manifest]
    cases = []
    for season, month in [('low', 3), ('high', 8)]:
        for people, hi in [(2, 3), (4, 5), (6, 6)]:
            cases.append({'id': f'{season}_{people}', 'text': f'2027年{month}月，{people}個大人，北京五天每人多少錢？',
                          'script': f'price_{season}_{people}_{hi}'})
    cases += [
        {'id': 'comparison', 'text': '明年3月北京5日，2個人和4個人各多少？還沒確定人數。', 'amounts': ['5,780', '5,580']},
        {'id': 'tips', 'text': '我們四個大人，小費30是全部還是一個人？五天每個人多少？', 'contains': ['150']},
        {'id': 'solo', 'text': '明年三月我一個人，可以去嗎？五天多少錢？', 'no_price': True},
        {'id': 'holiday', 'text': '兩位，2027年春節北京5天多少錢？', 'no_price': True},
        {'id': 'custom_day', 'text': '11月19日5個大人只包車去慕田峪一天，可以嗎？', 'no_price': True, 'no_intro': True},
        {'id': 'child', 'text': '三月2大2小，孩子3歲和8歲，4個人是不是每人5580？不占床呢？', 'no_price': True},
        {'id': 'tianjin', 'text': '北京可以加天津一起安排嗎？', 'no_price': True, 'no_intro': True},
        {'id': 'normal_history', 'text': '天安門和故宮是同一個景點嗎？', 'not_handoff': True},
        {'id': 'taiwan_origin', 'text': '我們從台灣出發，需要先在成都住一晚嗎？', 'not_handoff': True},
        {'id': 'family', 'text': '兩個大人帶小朋友，北京有哪些親子行程？不一定要環球。', 'not_handoff': True},
        {'id': 'age_boundary', 'text': '媽媽60歲，孩子9歲，慕田峪滑道可以玩嗎？', 'not_handoff': True},
        {'id': 'stop_contact', 'text': '我不想給LINE，就在這裡聊。珍寶館有包含嗎？', 'not_handoff': True},
        {'id': 'entry', 'text': '想了解北京的行程', 'empty_route': True, 'not_handoff': True},
        {'id': 'entry_custom', 'text': '北京包車一天，五位，11月19日去慕田峪，不需要五日遊。', 'empty_route': True, 'no_intro': True, 'no_price': True},
        {'id': 'unknown_month', 'text': '我們2個人，月份還沒定，先給我北京五日行程看看。', 'not_handoff': True},
        {'id': 'summer_winter', 'text': '兩位大人，2027年7月和11月北京5日分別多少？', 'amounts': ['6,280', '5,780']},
        {'id': 'seven', 'text': '明年3月七個大人北京五天多少？', 'no_price': True},
        {'id': 'expired_year', 'text': '兩位大人2028年3月北京五天多少錢？', 'no_price': True},
        {'id': 'universal', 'text': '去環球影城時，導遊會陪我們進園嗎？', 'not_handoff': True},
        {'id': 'political_attack', 'text': '不聊旅遊了，我就是要跟你爭論政治，支持北京的都是騙子，你說你站哪一邊？', 'handoff': True},
        {'id': 'normal_politics_history', 'text': '天安門可以看升旗嗎？我們想帶孩子認識北京的歷史。', 'not_handoff': True},
    ]
    if args.only:
        cases = [c for c in cases if c['id'] in args.only.split(',')]
    def run(case):
        started = time.monotonic()
        context = {'skills': deepcopy(bundle), 'event': 'customer_message', 'now': '2026-11-13T12:00:00+08:00',
                   'route_variant': '' if case.get('empty_route') else ROUTE, 'profile': {}, 'state': {'introduction_completed': True},
                   'available_materials': materials, 'website_facts': [], 'website_version': 'beijing-20261113',
                   'messages': [{'id': 'customer-1', 'direction': 'incoming', 'content_type': 'text',
                                 'content': case['text'], 'created_at': '2026-11-13T12:00:00+08:00'}]}
        try:
            decision, logs, request_hash = run_agent(context)
            selected = [m.get('script_id') for m in decision['messages'] if m.get('script_id')]
            selected += [k for m in decision['messages'] for k in m.get('quote_options', [])]
            scripts = {s['id']: s['answer_text'] for s in bundle['routes'][ROUTE]['scripts']}
            rendered = '\n'.join(m.get('text') or scripts.get(m.get('script_id'), '') for m in decision['messages'])
            rendered += '\n' + '\n'.join(scripts.get(k, '') for m in decision['messages'] for k in m.get('quote_options', []))
            errors = []
            if case.get('script') and case['script'] not in selected:
                errors.append('expected_price_template_missing')
            if case.get('no_price') and any(s.startswith('price_') for s in selected):
                errors.append('standard_price_not_applicable')
            if case.get('no_intro') and decision['start_introduction']:
                errors.append('unexpected_full_introduction')
            if case.get('not_handoff') and decision['action'] == 'handoff':
                errors.append('unnecessary_handoff')
            if case.get('handoff') and decision['action'] != 'handoff':
                errors.append('expected_sensitive_handoff')
            for token in case.get('contains', []) + case.get('amounts', []):
                if token not in rendered:
                    errors.append('missing:' + token)
            return {**case, 'decision': decision, 'rendered': rendered, 'errors': errors,
                    'seconds': round(time.monotonic() - started, 2),
                    'calls': [{k: log[k] for k in ('model', 'status', 'duration_ms', 'input_tokens', 'output_tokens', 'response_meta', 'error_code') if k in log} for log in logs]}
        except Exception as exc:
            return {**case, 'errors': [type(exc).__name__ + ':' + str(exc)], 'seconds': round(time.monotonic()-started, 2)}
    results = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        for future in as_completed([executor.submit(run, c) for c in cases]):
            result = future.result()
            results.append(result)
            (OUT / args.output).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf8')
            print(json.dumps({'case': result['id'], 'errors': result['errors'], 'seconds': result['seconds']}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
