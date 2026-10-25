"""Configured material eligibility and retry-stable sampling, using the shared catalog."""
import hashlib
import json


def itinerary_previews(bundle, materials, messages, profile=None):
    """Expose approved route-map references even before a route Skill is loaded.

    The existing material content-group roles identify maps. Filenames, route
    IDs and image appearance never decide which attachment is an itinerary.
    """
    from app.reception_v3.route_choice import rejects_current_route
    delivered = [m for m in messages if m.get('direction') == 'outgoing' and m.get('status') in
                 ('simulated_delivered', 'submitted', 'sent', 'delivered', 'read')]
    keys = {m.get('asset_key') for m in delivered} - {None, ''}
    hashes = {m.get('media_hash') for m in delivered} - {None, ''}
    ids = {m.get('media_id') for m in delivered} - {None}
    result = {}
    for route_id, route in bundle['routes'].items():
        if rejects_current_route(route_id, profile or {}):
            continue
        linked = {a for group in route.get('groups', {}).values() for a in group.get('assets', [])}
        maps = [m for m in materials if m.get('content_type') == 'image'
                and m.get('content_group_key') in ('itinerary_overview', 'itinerary')
                and route_id in m.get('routes', []) and m['key'] in linked]
        if maps:
            result[route_id] = {'name':route['name'], 'assets':[
                {'key':m['key'], 'name':m.get('name', ''), 'what_it_shows':m.get('what_it_shows', ''),
                 'already_delivered':(m['key'] in keys or m.get('media_hash') in hashes or m.get('media_id') in ids)}
                for m in maps]}
    return result


def rule_digest(rule):
    return hashlib.sha256(json.dumps(rule, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def eligible_rules(bundle, materials, route, labels, event, messages):
    purpose = 'followup' if event == 'silence_due' else 'reply'
    if event not in ('silence_due', 'customer_message'):
        return []
    sent = [m for m in messages if m.get('direction') == 'outgoing' and m.get('status') in
            ('simulated_delivered', 'submitted', 'sent', 'delivered', 'read')]
    used = {m.get('asset_key') for m in sent}
    hashes = {m.get('media_hash') for m in sent} - {None, ''}
    media_ids = {m.get('media_id') for m in sent} - {None}
    candidates = {m['key']: m for m in materials if route in m.get('routes', [])
                  and m.get('content_type') == 'image' and m['key'] not in used
                  and m.get('media_hash') not in hashes and m.get('media_id') not in media_ids}
    result = []
    for rule in bundle.get('material_rules', []):
        if (not rule['enabled'] or route not in rule['route_variants']
                or rule['purpose'] not in (purpose, 'both')
                or rule['any_labels'] and not set(rule['any_labels']).intersection(labels)):
            continue
        keys = list(dict.fromkeys(key for key in rule['asset_keys'] if key in candidates))
        # No usable picture: text remains available, without substituting a
        # different route's image or repeating a previously delivered image.
        result.append({**rule, 'asset_keys': keys})
    return result


def selected_asset(rule, session_id, generation):
    keys = rule['asset_keys']
    if not keys:
        return []
    seed = f'{session_id}:{generation}:{rule["id"]}'.encode()
    return [keys[int.from_bytes(hashlib.sha256(seed).digest()[:8], 'big') % len(keys)]]


def conversation_labels(db, row):
    if not row.conversation_state_id:
        return list(row.controls.get('evaluation_labels', [])) if row.environment != 'live' else []
    from app.models import ConversationState
    conversation = db.get(ConversationState, row.conversation_state_id)
    return list(set((conversation.labels or []) + ((conversation.contact.labels or []) if conversation.contact else []))) if conversation else []
