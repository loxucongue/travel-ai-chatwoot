"""Compile the configured route introduction into ordered delivery parts."""
from dataclasses import replace

from app.delivery_plan import ordered_delivery_parts


def introduction_group_keys(route: dict, slots: dict) -> list[str]:
    groups = route.get('groups', {})
    keys = [key for key in route.get('introduction_sequence', route.get('sequence', []))
            if groups.get(key, {}).get('initial_delivery')]
    party_size = slots.get('party_size')
    if not party_size and 'advisor_greeting' in keys and 'entry_question' in groups:
        # Ask within the introduction; missing party size does not block delivery.
        keys[keys.index('advisor_greeting')] = 'entry_question'
    if party_size and 'advisor_greeting' in keys:
        try:
            count = int(party_size)
        except (TypeError, ValueError):
            count = 0
        selected = ('party_intro_solo' if count == 1 else 'party_intro_small'
                    if 2 <= count <= 3 else 'party_intro_group' if count >= 4 else '')
        if selected in groups:
            keys[keys.index('advisor_greeting')] = selected
    return keys


def introduction_sections(route: dict, available: set[str], slots: dict) -> list[dict]:
    return sections_for_groups(route, introduction_group_keys(route, slots), available)


def sections_for_groups(route: dict, keys: list[str], available: set[str]) -> list[dict]:
    groups = route.get('groups', {})
    sections = []
    for key in keys:
        group = groups.get(key)
        if not group or not group.get('text'):
            raise ValueError('v2_introduction_group_unavailable:' + key)
        assets = list(group.get('assets') or [])
        if not set(assets) <= available:
            raise ValueError('v2_introduction_material_unavailable:' + key)
        sections.append({'group_key': key, 'text': group['text'], 'asset_keys': assets,
                         'evidence_refs': list(group.get('evidence') or []),
                         'delivery_mode': group.get('delivery_mode') or 'text_then_assets'})
    return sections


def introduction_parts(sections: list[dict], materials: list[dict], *, plan_id: str,
                       interval_seconds: float) -> list:
    """Use frozen section content and already approved transport material snapshots."""
    by_key = {str(item.get('asset_key') or item.get('key')): item for item in materials}
    result = []
    for index, section in enumerate(sections):
        # A previously confirmed image may have been removed by the executor's
        # deduplication gate; do not recreate or resolve it from a live catalog.
        selected = [by_key[key] for key in section['asset_keys'] if key in by_key]
        parts = ordered_delivery_parts(section['text'], selected, section['delivery_mode'],
            plan_id=f'{plan_id}:section:{index}', content_group_key=section['group_key'],
            interval_seconds=interval_seconds)
        if result and parts:
            parts[0] = replace(parts[0], interval_seconds=interval_seconds)
        result.extend(parts)
    return result
