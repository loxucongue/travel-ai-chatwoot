import pytest

from app.reception_v2.material_delivery import introduction_parts, introduction_sections
from app.route_packages import ROUTES


@pytest.mark.parametrize('route', ['peach_9d_2027', 'peach_11d_2027'])
def test_full_introduction_orders_actual_parts_and_scopes_receipts(route):
    spec = ROUTES[route]
    available = {key for group in spec['groups'].values() for key in group['assets']}
    sections = introduction_sections(spec, available, {})
    keys = [section['group_key'] for section in sections]
    assert keys[:2] == ['entry_question', 'itinerary_overview']
    assert keys[-2:] == ['accommodation_summary', 'no_shopping']
    if route == 'peach_11d_2027':
        assert keys.index('rongbuk_reference') < keys.index('hotel_reference')
    materials = [{'asset_key': key, 'media_hash': key} for key in available]
    parts = introduction_parts(sections, materials, plan_id='request:1', interval_seconds=2)
    assert parts[0].content == spec['groups']['entry_question']['text']
    first_image = next(part for part in parts if part.kind == 'media')
    assert first_image.material['asset_key'] in spec['groups']['itinerary_overview']['assets']
    assert parts[0].interval_seconds == 0
    assert all(part.interval_seconds == 2 for part in parts[1:])
    assert len({part.part_id for part in parts}) == len(parts)
    assert introduction_parts(sections, materials, plan_id='request:1', interval_seconds=2) == parts
    assert all(part.content_group_key in keys for part in parts)
    assert 'party_question' not in [s['group_key'] for s in introduction_sections(spec, available, {'party_size': 4})]


def test_partial_catalog_never_masquerades_as_complete_introduction():
    with pytest.raises(ValueError, match='material_unavailable'):
        introduction_sections(ROUTES['peach_9d_2027'], {'routes12-9d-itinerary'}, {})


def test_confirmed_image_dedup_does_not_remove_other_sections():
    spec = ROUTES['peach_9d_2027']
    available = {key for group in spec['groups'].values() for key in group['assets']}
    sections = introduction_sections(spec, available, {'party_size': 4})
    parts = introduction_parts(sections, [], plan_id='already-delivered', interval_seconds=1)
    assert [p.content_group_key for p in parts] == [s['group_key'] for s in sections]
    assert all(part.kind == 'text' for part in parts)


def test_introduction_receipt_does_not_mark_all_facts_provided_on_first_message():
    from app.reception_v2.events import answer_receipt
    sections = [{'group_key': 'brand_positioning', 'text': '品牌介绍', 'evidence_refs': ['brand']},
                {'group_key': 'hotel_reference', 'text': '住宿介绍', 'evidence_refs': ['hotel']}]
    decision = {'action': 'reply', 'route_variant': 'peach_9d_2027',
                'reply': '品牌介绍', 'evidence_refs': ['brand', 'hotel'], 'v2_delivery_sections': sections}
    assert answer_receipt(decision, '品牌介绍')['fact_ids'] == ['brand']
    assert answer_receipt(decision, '住宿介绍')['fact_ids'] == ['hotel']
    assert answer_receipt(decision, '未计划的文字') is None
