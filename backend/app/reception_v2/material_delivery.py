"""Compile an explicit full-introduction request into reviewed, interruptible parts."""
from dataclasses import replace

from app.delivery_plan import ordered_delivery_parts


def introduction_sections(route: dict, available: set[str], slots: dict) -> list[dict]:
    groups = route.get('groups', {})
    keys = ['brand_positioning', 'itinerary_overview', 'hotel_reference']
    if 'rongbuk_reference' in groups:
        keys.append('rongbuk_reference')
    keys.append('vehicle_reference')
    if not slots.get('party_size'):
        keys.append('party_question')
    return sections_for_groups(route, keys, available)


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
