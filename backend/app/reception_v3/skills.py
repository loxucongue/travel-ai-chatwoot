"""Compile current configuration into a complete route Skill, without V2 rules."""
import hashlib
import json
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
import re
from zoneinfo import ZoneInfo

import yaml

from app.reception_config import get_reception_configuration
from app.route_packages import ROUTES, PACKAGE_ROOT, ensure_route_packages_current


def service_knowledge():
    return json.loads((PACKAGE_ROOT.parent / 'service-knowledge/service-knowledge.json').read_text(encoding='utf-8'))


def effective_price_scripts(scripts, now=None, offer=None):
    """Render validity in code; a previously issued offer never rolls forward."""
    today = (now or datetime.now(ZoneInfo('Asia/Taipei'))).astimezone(ZoneInfo('Asia/Taipei')).date().isoformat()
    result = deepcopy(scripts)
    for script in result:
        if script.get('id') == 'price' and script.get('valid_for_days'):
            promise = offer or {'first_quoted_on': today, 'valid_until': (
                datetime.fromisoformat(today) + timedelta(days=script['valid_for_days'])).date().isoformat()}
            expired = today > promise['valid_until']
            deadline = datetime.fromisoformat(promise['valid_until']).strftime('%Y/%m/%d')
            script['answer_text'] = script['fallback_text'] if expired else script['answer_text'].replace('{offer_deadline}', deadline)
            script['quote_offer'] = deepcopy(promise)
            script['usage_note'] = (script.get('usage_note', '') + '\n使用本次报价原文。截止日由系统按首次报价计算，同一客户固定；过期用标准价，不重新延长。').strip()
            continue
        if script.get('id') != 'price' or not script.get('valid_until'):
            continue
        if today > script['valid_until'] or today < (script.get('valid_from') or '0001-01-01'):
            script['answer_text'] = script['fallback_text']
        script['usage_note'] = (script.get('usage_note', '') + '\n本次生效报价如下。只报本线路，原样使用；不得根据历史回复延长活动或使用过期价格。').strip()
    return result


def compile_skills(db, now=None):
    ensure_route_packages_current()
    config = get_reception_configuration(db)
    service = service_knowledge()
    common_scripts = {s['id']: {
        'id': s['id'], 'name': s['name'], 'text': s['answer_text'], 'enabled': True,
        'scenario': json.dumps({'适用': s.get('positive_examples', []),
                               '不适用': s.get('negative_examples', [])}, ensure_ascii=False),
    } for s in service.get('fixed_answers', []) if s.get('status') == 'active'}
    # An operator override, including a disabled script, wins by ID.
    common_scripts.update({s['id']: deepcopy(s) for s in config.get('common_scripts', [])})
    routes = {}
    for key, route in ROUTES.items():
        if key not in config['routing']['enabled_route_variants']:
            continue
        routes[key] = {
            'name': route['name'], 'aliases': list(dict.fromkeys(filter(None, [
                *route.get('selection_aliases', []), route.get('selection_title', ''),
                route.get('default_entry_message', '')]))),
            'facts': deepcopy(route['knowledge_facts']),
            'guidance': route.get('ai_guidance', ''),
            'routing_guidance': route.get('routing_guidance', ''),
            'followup_policy': deepcopy(route.get('followup_policy')),
            'intake': deepcopy(route.get('intake')),
            'entry_comparison': deepcopy(route.get('entry_comparison')),
            'seasonal_pricing': deepcopy(route.get('seasonal_pricing')),
            'departure_availability': deepcopy(route.get('departure_availability')),
            'large_party': deepcopy(route.get('large_party')),
            'profile_labels': deepcopy(route.get('profile_labels', {})),
            'opening_text_in_introduction': route.get('opening_text_in_introduction', False),
            'scripts': [{k: deepcopy(s[k]) for k in ('id', 'name', 'answer_text', 'asset_ids',
                         'positive_examples', 'negative_examples', 'party_size_min', 'party_size_max',
                         'topics', 'usage_note', 'status', 'quote_offer', 'answer_scope') if k in s}
                        for s in effective_price_scripts(route.get('fixed_answers', []), now) if s.get('status') == 'active'],
            'groups': deepcopy(route['groups']),
            'introduction_sequence': [k for k in route['introduction_sequence']
                                      if route['groups'][k].get('initial_delivery')],
            'switch_introduction_sequence': deepcopy(route.get('switch_introduction_sequence')),
            'interval_seconds': route['initial_delivery_interval_seconds'],
            'version': route['package_version'],
            'quote_policy': next((deepcopy(s) for s in route.get('fixed_answers', [])
                                  if s.get('id') == 'price' and s.get('valid_for_days') and s.get('status') == 'active'), None),
        }
        from app.reception_v3.seasonal_quotes import apply_group_price_scripts
        apply_group_price_scripts(routes[key])
        if route.get('party_pricing'):
            from app.reception_v3.party_quotes import apply_price_scripts
            routes[key]['party_pricing'] = deepcopy(route['party_pricing'])
            routes[key]['party_pricing_enabled'] = any(s['id'] == 'price' for s in routes[key]['scripts'])
            apply_price_scripts(routes[key])
        price = next((s for s in routes[key]['scripts'] if s['id'] == 'price'), None)
        if price:
            routes[key]['facts'].append({'id': key + '.current_quote', 'text': '本轮当前有效报价（优先于历史价格）：\n' + price['answer_text']})
            for group in ('price_reference', 'price_deferral'):
                if group in routes[key]['groups']:
                    routes[key]['groups'][group]['text'] = price['answer_text']
    if any(r.get('seasonal_pricing') for r in routes.values()):
        for route in routes.values():
            route['aliases'] = [a for a in route['aliases'] if a not in ('有珠峰的', '包含珠峰')]
    bundle = {'routes': routes, 'common_scripts': list(common_scripts.values()),
              'material_rules': deepcopy(config.get('material_rules', [])),
              'service_knowledge': {'version': service['knowledge_version'], 'facts': service['facts']},
              'reply': config['reply'], 'intake': config['intake'], 'silence': {k: v for k, v in config['silence'].items()
                                                  if not k.startswith('v2_')},
              'lead_capture': config['lead_capture'], 'routing': config['routing'], 'handoff': config['handoff']}
    bundle['digest'] = hashlib.sha256(json.dumps(bundle, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return bundle


SKILL_ROOT = Path(__file__).with_name('skills')


class SkillRegistry:
    """Standard SKILL.md index; current configuration is attached only on load."""

    def __init__(self, bundle, materials=(), root=SKILL_ROOT, profile=None):
        self.bundle, self.materials = bundle, materials
        self.profile = profile or {}
        self.skills = {}
        for path in sorted(root.glob('*/SKILL.md')):
            text = path.read_text(encoding='utf-8')
            sections = text.split('---', 2)
            if len(sections) != 3 or sections[0].strip():
                raise ValueError(f'skill_frontmatter_missing:{path}')
            meta = yaml.safe_load(sections[1])
            name = meta.get('name', '') if isinstance(meta, dict) else ''
            description = meta.get('description', '') if isinstance(meta, dict) else ''
            if (not isinstance(name, str) or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', name)
                    or len(name) > 64 or name != path.parent.name
                    or not isinstance(description, str) or not description.strip() or len(description) > 1024):
                raise ValueError(f'skill_metadata_invalid:{path}')
            if name in self.skills:
                raise ValueError(f'skill_name_duplicate:{name}')
            self.skills[name] = {'name': name, 'description': description, 'body': sections[2].strip()}

    def index(self):
        return [{k: item[k] for k in ('name', 'description')} for item in self.skills.values()
                if item['name'] == 'tibet-reception' or item['name'].replace('-', '_') in self.bundle['routes']]

    def route_skill(self, route):
        name = route.replace('_', '-')
        return name if route in self.bundle['routes'] and name in self.skills else None

    def load(self, name):
        item = self.skills.get(name)
        if item is None:
            raise ValueError(f'skill_not_found:{name}')
        route_id = name.replace('-', '_')
        route = self.bundle['routes'].get(route_id)
        body = item['body']
        if name == 'tibet-reception':
            # Only settings used by V3 reasoning belong in the model context.
            # Opening content and delivery limits are executed by the service;
            # legacy topic-count triggers must not compete with the Skill.
            fields = {
                'reply': ('goal', 'tone', 'tone_guidance', 'custom_guidance'),
                'intake': ('enabled', 'question', 'options', 'wait_seconds'),
                'silence': ('material_selection', 'enabled', 'daily_at', 'intervals_minutes', 'initial_checkpoints_minutes', 'recurring_interval_minutes', 'active_start', 'active_end'),
                'handoff': ('large_group_enabled', 'large_group_minimum'),
                'routing': ('enabled_route_variants', 'allow_route_switch', 'preserve_profile_on_switch', 'outside_catalog_action'),
                'lead_capture': ('enabled', 'channels', 'after_contact', 'customer_initiated_channels', 'require_supported_route',
                                 'require_party_size', 'require_departure_window',
                                 'next_day_invitation_enabled', 'consultation_turn_threshold'),
            }
            common = {section: {key: self.bundle.get(section, {}).get(key)
                                for key in keys if key in self.bundle.get(section, {})}
                      for section, keys in fields.items()}
            body += '\n\n## 当前接待配置\n' + json.dumps(common, ensure_ascii=False)
            body += '\n\n## 通用场景原话\n' + '\n\n'.join(
                f"### {s.get('name', s['id'])} [{s['id']}]\n适用场景：{s.get('scenario', '')}\n原话：\n{s.get('text', '')}"
                for s in self.bundle.get('common_scripts', []) if s.get('enabled', True))
        elif route is not None:
            body += f"\n\n## 当前线路\nID：{route_id}\n版本：{route['version']}"
            if route.get('intake'):
                body += '\n线路专属开场问题（覆盖通用人数开场）：' + json.dumps(route['intake'], ensure_ascii=False)
            if route.get('entry_comparison'):
                body += '\n同时询问配对线路时的默认展示顺序（明确偏好、天数和日期优先）：' + json.dumps(route['entry_comparison'], ensure_ascii=False)
            if route.get('seasonal_pricing'):
                body += '\n分季报价规则：' + json.dumps(route['seasonal_pricing'], ensure_ascii=False)
                if route['seasonal_pricing'].get('group_pricing'):
                    body += '\n本線已啟用季節＋人數＋團型核價，優先於舊版模板與不疊加多人優惠的說明。'
                    body += '每次問價或改問人數（包括假設六人、比較四人六人），都重新調用get_departure_quote並明確傳party_size與group_type；不能沿用歷史金額或以同價省略核價。'
                    body += '未定團型傳undecided，工具可返回拼團與獨團options，直接比較，不逼問先選。六人起按獨團介紹優勢；七人以上完整費用未配置須顧問核價。'
                    body += '只引用本輪核價options中的quote_script_id，單方案script_id、多方案quote_options，text留空；日期條件另用text，不能引用未扣優惠的price_low等季節模板。'
                    body += '金額已含優惠，不再相減，不稱原價；六人說明不拼其他人、可另選出發日期，新日期仍須核價及確認名額。'
                    body += '假設人數僅為比較，不更新已確認報名人數。歷史錯價不能當已承諾優惠沿用。早鳥按本輪工具狀態判斷，未確認不得自行疊加，也不說公司沒有優惠。'
            if route.get('party_pricing'):
                body += '\n人數報價已由系統計算，單一人數用對應price_*模板，多種人數比較用quote_options；比較的最終每人價不同時可另用text按pricing_evidence解釋每人優惠及已包含在最終價，不要求先確定報名人數。未選團型按明示拼團價提供參考，不據此標記客人已選拼團。未配置人數不猜折扣；不展示早鳥或限時文案。對客稱優惠價，不稱原價；同價只承接上述優惠價，不重複金額、不主動展開其他檔位，歷史已報過且仍適用時不重發整套報價。'
                body += '\n單人報價或單人確認接受拼房，按通用Skill補充未拼成需補單房差及有依據的本線金額；這是未問房差通常不主動提的例外，不代表必須單住或保證拼房成功。多人未問房型不問分房。必要說明放在輕鬆收尾問句前。客人變更人數／團型只記錄諮詢意向，不代表訂位或實際改團。人數未定時可以先比較有依據的方案，最終安排等客人確認，不能說幫客人保留名額、已改好團，也不能自行給確認期限（例如四月初前、幾天內）。未問期限不新增日期；只有工具公布的團期可以引用。'
            if route.get('departure_availability'):
                body += '\n已公布出发团期（不代表库存保证）：' + json.dumps(route['departure_availability'], ensure_ascii=False)
            if route.get('guidance'):
                body += '\n接待说明：' + route['guidance']
            if route.get('followup_policy'):
                body += '\n本線專用跟進檢查點（優先於普通每日時鐘，仍受發送權限與窗口限制）：' + json.dumps(route['followup_policy'], ensure_ascii=False)
            if (route.get('large_party') or {}).get('enabled'):
                body += '\n优先适用的独立成团介绍后交接规则（人数门槛及明确包团是否适用均以配置为准，覆盖旧直接交接句）：' + json.dumps(route['large_party'], ensure_ascii=False)
            body += f"\n\n## 完整介绍原文\n逐条间隔：{route['interval_seconds']} 秒\n"
            for index, key in enumerate(route['introduction_sequence'], 1):
                from app.reception_v3.seasonal_quotes import seasonal_group
                group = seasonal_group(route['groups'][key], self.profile)
                body += (f"\n### {index}. {key}\n{group['text']}\n"
                         f"素材：{json.dumps(group.get('assets', []), ensure_ascii=False)}\n"
                         f"图文顺序：{group.get('delivery_mode', 'assets_then_text')}\n")
                if group.get('seasonal_override'):
                    body += '季節替換（優先於上文，冬季/日期未知不發此組客房圖）：' + json.dumps(group['seasonal_override'], ensure_ascii=False) + '\n'
            body += '\n## 场景原话\n' + '\n\n'.join(
                f"### {s.get('name', s['id'])} [{s['id']}]\n"
                + '适用场景：' + json.dumps(s.get('positive_examples', []), ensure_ascii=False)
                + '\n不适用示例：' + json.dumps(s.get('negative_examples', []), ensure_ascii=False)
                + '\n使用条件：' + json.dumps({k: s[k] for k in
                    ('party_size_min', 'party_size_max', 'topics', 'usage_note') if k in s}, ensure_ascii=False)
                + '\n' + s['answer_text'] + '\n素材：' + json.dumps(s.get('asset_ids', []), ensure_ascii=False)
                for s in route.get('scripts', [])
                if not ((route.get('seasonal_pricing') or {}).get('group_pricing')
                        and s['id'] in {b['script_id'] for b in route['seasonal_pricing']['bands']}))
            body += '\n\n## 线路事实\n' + '\n'.join(
                f"- [{f.get('id', '')}] {f.get('text', '')}" for f in route.get('facts', []))
            body += '\n\n## 可用素材\n' + json.dumps(
                [m for m in self.materials if route_id in m.get('routes', [])], ensure_ascii=False)
        else:
            raise ValueError(f'skill_route_configuration_missing:{name}')
        if route and (route.get('seasonal_pricing') or {}).get('group_pricing'):
            body += ('\n\n## 本輪答覆範圍（取代舊完整報價及收尾問句慣例）\n'
                     '依本輪問題選用一種版式，不是全部輸出：首次報價列短行價格及共同包含／不含；'
                     '只追問另一人數時，直接給新每人價、該人數優惠與獨團優勢，約兩三句即可，'
                     '不要重發完整包含／不含、交通清單、房型設施或小費。客人只問交通就只答交通。'
                     '同時問價格與交通就價格列表加交通短段。不要抄前面完整報價來湊答案。'
                     '未核准人數或團型的價格不能用其他人數代答；不額外推薦其他人數。'
                     '早鳥尚無核准計算條件時說需要確認，不說公司沒有、不說一定不能優惠。'
                     '說完即可，不強制問句；只在總價被問到時列整團合計。')
        return {'name': name, 'description': item['description'], 'instructions': body,
                'digest': hashlib.sha256(body.encode()).hexdigest()}
