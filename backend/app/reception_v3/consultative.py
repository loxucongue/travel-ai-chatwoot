"""Evaluation-only, single-owner reception experiment.

The model interprets speech and writes the answer in the same call. Python checks
IDs, provenance and execution contracts; it does not classify customer phrases.
No production activation path is provided by this pilot.
"""
import hashlib
import json
import os
from pathlib import Path

from app.config import settings
from app.reception_v3 import route_understanding as understanding

VERSION = 'consultative-pilot-4'
BRIEF = Path(__file__).with_name('consultative_brief.md')


def enabled():
    return bool(settings.reception_consultative_pilot_enabled
                and settings.app_profile == 'evaluation'
                and not settings.outbound_enabled and not settings.chatwoot_write_enabled
                and os.environ.get('BUSINESS_PROFILE', 'china2go') == 'china2go')


def product_document(registry, name):
    """Read current data without injecting another route's reception SOP."""
    route_id = name.replace('-', '_')
    if name == 'tibet-reception':
        fields = {
            'reply': ('goal', 'tone', 'tone_guidance', 'custom_guidance'),
            'routing': ('enabled_route_variants', 'allow_route_switch', 'outside_catalog_action'),
            'handoff': ('large_group_enabled', 'large_group_minimum'),
            'lead_capture': ('enabled', 'channels', 'require_supported_route', 'require_party_size',
                             'require_departure_window', 'next_day_invitation_enabled', 'consultation_turn_threshold'),
            'silence': ('enabled', 'material_selection'),
        }
        data = {s: {k: registry.bundle.get(s, {}).get(k) for k in keys
                    if k in registry.bundle.get(s, {})} for s, keys in fields.items()}
        data['common_scripts'] = [s for s in registry.bundle.get('common_scripts', []) if s.get('enabled', True)]
    else:
        route = registry.bundle['routes'].get(route_id)
        if route is None or registry.route_skill(route_id) != name:
            raise ValueError('skill_route_configuration_missing:' + name)
        data = {'route_id': route_id, **{k: route[k] for k in (
            'name', 'version', 'facts', 'intake', 'departure_availability', 'large_party',
            'party_pricing', 'party_pricing_enabled') if k in route}}
        # Templates are system-rendered. Their IDs and conditions must stay visible.
        data['scripts'] = [{k: s[k] for k in ('id', 'name', 'answer_text', 'party_size_min',
            'party_size_max', 'usage_note', 'asset_ids') if k in s} for s in route.get('scripts', [])
            if not s['id'].startswith('price')]
        data['introduction_available'] = bool(route.get('introduction_sequence'))
        data['pricing_available_via'] = 'read_price_templates'
        data['facts'] = [f for f in data.get('facts', []) if not f.get('id', '').endswith('.current_quote')]
        data.pop('party_pricing', None)
    data['document_kind'] = 'configured_reference_not_conversation'
    body = json.dumps(data, ensure_ascii=False)
    return {'name': name, 'description': '当前已配置业务资料；读取不代表推荐、选线或发送。',
            'instructions': body, 'digest': hashlib.sha256(body.encode()).hexdigest()}


def prompt(index, common, preloaded):
    return (BRIEF.read_text(encoding='utf-8')
            + '\n## 资料目录\n' + json.dumps(index, ensure_ascii=False)
            + '\n## 当前配置\n' + common['instructions']
            + '\n## 当前产品资料\n' + json.dumps(preloaded, ensure_ascii=False))


def attach_task(current, context):
    data = understanding.task(context)
    # Route facts are in each product document, not repeated for every product
    # in the initial task. Let the model choose additional documents to read.
    current.pop('route_facts', None)
    current.pop('pricing_evidence', None)
    current['dialogue_evidence'] = data
    current['decision_owner'] = VERSION
    return current


def unpack(raw, context):
    """Validate same-call interpretation before existing execution checks.

    Evidence verifies source, not semantic correctness. Model judgment still
    needs multi-turn quality evaluation; this is not a proof of understanding.
    """
    raw = dict(raw)
    selection = raw.pop('selection_evidence', None)
    memory = raw.pop('memory_evidence', {})
    data = understanding.task(context)
    if context.get('event') == 'customer_message':
        selecting = raw.get('route_choice') == 'selected'
        current = [m for m in data['history'] if m['id'] in data['current_message_ids']]
        # This adapter records the model's chosen action, not a second opinion
        # on what customer text means. Normal replies need no intent taxonomy.
        evidence = [selection] if selecting else [
            {'message_id': m['id'], 'quote': m['text'][:1600]} for m in current if m['text']][-1:]
        interpreted = understanding.validate({'intent': 'select' if selecting else 'other',
            'selected_route': raw.get('route_variant', '') if selecting else '',
            'evidence': evidence}, data) if evidence else None
    else:
        if selection is not None:
            raise ValueError('timer_has_no_new_customer_selection')
        interpreted = None
    if not isinstance(memory, dict):
        raise ValueError('memory_evidence_must_be_object')
    sources = {m['id']: m for m in data['history'] if m['speaker'] == 'customer'
               and m['id'] in data['current_message_ids']}
    changes = dict(raw.get('profile', {}))
    if raw.get('opt_out') is not None and raw['opt_out'] != context.get('state', {}).get('opt_out', False):
        changes['opt_out'] = raw['opt_out']
    for key, value in changes.items():
        if key == 'discussion_route' or value == context.get('profile', {}).get(key):
            continue
        evidence = memory.get(key)
        if context.get('event') != 'customer_message' or not isinstance(evidence, dict):
            raise ValueError('new_profile_value_requires_customer_source:' + key)
        source = sources.get(evidence.get('message_id'))
        quote = evidence.get('quote')
        if not source or not isinstance(quote, str) or not quote or quote not in source['text']:
            raise ValueError('invalid_profile_source:' + key)
    log = None if interpreted is None else {
        'node': 'route_understanding', 'version': understanding.VERSION, 'status': 'completed',
        'owner': VERSION, 'interpretation': interpreted, 'customer_message_ids': data['current_message_ids'],
        'profile_sources': memory,
    }
    return raw, interpreted, log


def price_templates(bundle, route_id):
    """Prices have their own lookup; no customer text is inspected here."""
    from app.reception_v3.party_quotes import pricing_evidence
    route = bundle['routes'][route_id]
    return {'route_id': route_id, 'version': route['version'],
            'scripts': [s for s in route.get('scripts', []) if s['id'].startswith('price')],
            'pricing_evidence': pricing_evidence({'routes': {route_id: route}}).get(route_id),
            'note': '价目资料不是发送或库存授权；核对客人日期后，按本轮实际询价选择模板。'}


def repair_schema(base):
    """The correction must describe the same protocol as the original task."""
    schema = dict(base)
    schema['properties'] = {**base['properties'],
        'selection_evidence': {'type': 'object', 'description': 'route_choice=selected时必填，客户本轮选线原话',
            'properties': {'message_id': {'type': 'string'}, 'quote': {'type': 'string'}},
            'required': ['message_id', 'quote'], 'additionalProperties': False},
        'memory_evidence': {'type': 'object', 'description': 'profile新增/更新及opt_out改变的字段名映射到本轮客户的message_id和quote'}}
    return schema
