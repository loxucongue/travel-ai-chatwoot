"""Compile current configuration into a complete route Skill, without V2 rules."""
import hashlib
import json
from copy import deepcopy

from app.reception_config import get_reception_configuration
from app.route_packages import ROUTES, ensure_route_packages_current


def compile_skills(db):
    ensure_route_packages_current()
    config = get_reception_configuration(db)
    routes = {}
    for key, route in ROUTES.items():
        routes[key] = {
            'name': route['name'], 'aliases': route.get('selection_aliases', []),
            'facts': deepcopy(route['knowledge_facts']),
            'scripts': [{k: deepcopy(s[k]) for k in ('id', 'name', 'answer_text', 'asset_ids', 'positive_examples', 'status') if k in s}
                        for s in route.get('fixed_answers', []) if s.get('status') == 'active'],
            'groups': deepcopy(route['groups']),
            'introduction_sequence': [k for k in route['introduction_sequence']
                                      if route['groups'][k].get('initial_delivery')],
            'interval_seconds': route['initial_delivery_interval_seconds'],
            'version': route['package_version'],
        }
    bundle = {'routes': routes, 'common_scripts': config.get('common_scripts', []),
              'reply': config['reply'], 'silence': {k: v for k, v in config['silence'].items()
                                                  if not k.startswith('v2_')},
              'lead_capture': config['lead_capture']}
    bundle['digest'] = hashlib.sha256(json.dumps(bundle, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return bundle
