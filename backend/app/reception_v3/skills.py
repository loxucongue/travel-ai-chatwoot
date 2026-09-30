"""Compile current configuration into a complete route Skill, without V2 rules."""
import hashlib
import json
from copy import deepcopy
from pathlib import Path
import re

import yaml

from app.reception_config import get_reception_configuration
from app.route_packages import ROUTES, PACKAGE_ROOT, ensure_route_packages_current


def service_knowledge():
    return json.loads((PACKAGE_ROOT.parent / 'service-knowledge/service-knowledge.json').read_text(encoding='utf-8'))


def compile_skills(db):
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
            'name': route['name'], 'aliases': route.get('selection_aliases', []),
            'facts': deepcopy(route['knowledge_facts']),
            'guidance': route.get('ai_guidance', ''),
            'scripts': [{k: deepcopy(s[k]) for k in ('id', 'name', 'answer_text', 'asset_ids',
                         'positive_examples', 'negative_examples', 'party_size_min', 'party_size_max',
                         'topics', 'usage_note', 'status') if k in s}
                        for s in route.get('fixed_answers', []) if s.get('status') == 'active'],
            'groups': deepcopy(route['groups']),
            'introduction_sequence': [k for k in route['introduction_sequence']
                                      if route['groups'][k].get('initial_delivery')],
            'interval_seconds': route['initial_delivery_interval_seconds'],
            'version': route['package_version'],
        }
    bundle = {'routes': routes, 'common_scripts': list(common_scripts.values()),
              'service_knowledge': {'version': service['knowledge_version'], 'facts': service['facts']},
              'reply': config['reply'], 'silence': {k: v for k, v in config['silence'].items()
                                                  if not k.startswith('v2_')},
              'lead_capture': config['lead_capture'], 'routing': config['routing'], 'handoff': config['handoff']}
    bundle['digest'] = hashlib.sha256(json.dumps(bundle, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return bundle


SKILL_ROOT = Path(__file__).with_name('skills')


class SkillRegistry:
    """Standard SKILL.md index; current configuration is attached only on load."""

    def __init__(self, bundle, materials=(), root=SKILL_ROOT):
        self.bundle, self.materials = bundle, materials
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
                'silence': ('enabled', 'intervals_minutes'),
                'handoff': ('large_group_enabled', 'large_group_minimum'),
                'routing': ('enabled_route_variants', 'allow_route_switch', 'preserve_profile_on_switch', 'outside_catalog_action'),
                'lead_capture': ('enabled', 'channels', 'require_supported_route',
                                 'require_party_size', 'require_departure_window'),
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
            if route.get('guidance'):
                body += '\n接待说明：' + route['guidance']
            body += f"\n\n## 完整介绍原文\n逐条间隔：{route['interval_seconds']} 秒\n"
            for index, key in enumerate(route['introduction_sequence'], 1):
                group = route['groups'][key]
                body += (f"\n### {index}. {key}\n{group['text']}\n"
                         f"素材：{json.dumps(group.get('assets', []), ensure_ascii=False)}\n"
                         f"图文顺序：{group.get('delivery_mode', 'assets_then_text')}\n")
            body += '\n## 场景原话\n' + '\n\n'.join(
                f"### {s.get('name', s['id'])} [{s['id']}]\n"
                + '适用场景：' + json.dumps(s.get('positive_examples', []), ensure_ascii=False)
                + '\n不适用示例：' + json.dumps(s.get('negative_examples', []), ensure_ascii=False)
                + '\n使用条件：' + json.dumps({k: s[k] for k in
                    ('party_size_min', 'party_size_max', 'topics', 'usage_note') if k in s}, ensure_ascii=False)
                + '\n' + s['answer_text'] + '\n素材：' + json.dumps(s.get('asset_ids', []), ensure_ascii=False)
                for s in route.get('scripts', []))
            body += '\n\n## 线路事实\n' + '\n'.join(
                f"- [{f.get('id', '')}] {f.get('text', '')}" for f in route.get('facts', []))
            body += '\n\n## 可用素材\n' + json.dumps(
                [m for m in self.materials if route_id in m.get('routes', [])], ensure_ascii=False)
        else:
            raise ValueError(f'skill_route_configuration_missing:{name}')
        return {'name': name, 'description': item['description'], 'instructions': body,
                'digest': hashlib.sha256(body.encode()).hexdigest()}
