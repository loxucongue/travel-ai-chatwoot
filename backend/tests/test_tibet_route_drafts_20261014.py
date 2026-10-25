"""Validate the provided maps without activating unverified prices or scripts."""
import json
from pathlib import Path

import pytest

from app.route_packages import PACKAGE_ROOT, ROUTE_PACKAGES, _validate


DRAFT_ROOT = PACKAGE_ROOT.parent / 'route-drafts' / '20261014'
ASSETS = json.loads((DRAFT_ROOT / 'asset-manifest.json').read_text(encoding='utf8'))


@pytest.mark.parametrize('folder,route,days,count', [
    ('tibet-essence-8d', 'tibet_essence_8d', 8, 14),
    ('tibet-overview-10d', 'tibet_overview_10d', 10, 18),
])
def test_map_draft_complete_but_not_activated(folder, route, days, count):
    path = DRAFT_ROOT / folder / 'route-package.json'
    draft = _validate(json.loads(path.read_text(encoding='utf8')), path)
    itinerary = json.loads((path.parent / 'itinerary.json').read_text(encoding='utf8'))
    assert draft['route_variant'] == route
    assert route not in ROUTE_PACKAGES
    assert draft['publication']['activation_allowed'] is False
    assert draft['publication']['blockers']
    assert len(itinerary) == days
    assert [d['day'] for d in itinerary] == list(range(1, days + 1))
    assets = [a for a in ASSETS if a['route_variant'] == route]
    assert len(assets) == count
    keys = {a['asset_key'] for a in assets}
    references = {k for g in draft['content_groups'].values() for k in g['asset_keys']}
    assert keys == references
    assert all(a['available'] is False for a in assets)
    assert draft['fixed_answers'] == []  # No stale or borrowed quotation.
    assert all(a['route_variant'] == route for a in assets if a['asset_key'] in references)
    assert draft['source']['map_sha256'] == next(a['sha256'] for a in assets if a['content_group_key'] == 'itinerary_overview')


def test_two_routes_do_not_share_their_itinerary_images():
    maps = [a for a in ASSETS if a['content_group_key'] == 'itinerary_overview']
    assert len(maps) == 2
    assert len({a['sha256'] for a in maps}) == 2
    assert len({a['asset_key'] for a in ASSETS}) == 32


def test_arrival_and_everest_hotel_remain_route_specific():
    eight = json.loads((DRAFT_ROOT / 'tibet-essence-8d' / 'itinerary.json').read_text(encoding='utf8'))
    ten = json.loads((DRAFT_ROOT / 'tibet-overview-10d' / 'itinerary.json').read_text(encoding='utf8'))
    assert '拉薩' in eight[0]['places'] and '林芝' not in eight[0]['places']
    assert '林芝' in ten[0]['places']
    for days, everest_index in [(eight, 4), (ten, 6)]:
        assert '珠峰大本營' in days[everest_index]['places']
        assert '絨布旅館' in days[everest_index]['hotel']
        assert '希爾頓' not in days[everest_index]['hotel']
        assert '拉日鐵路' in days[-2]['places']
        assert '拉薩' in days[-1]['places']


def test_followup_photos_are_bound_but_not_in_the_initial_wave():
    for path in DRAFT_ROOT.glob('*/route-package.json'):
        package = json.loads(path.read_text(encoding='utf8'))
        for group in ('followup_sakya', 'followup_railway'):
            assert package['content_groups'][group]['asset_keys']
            assert package['content_groups'][group]['initial_delivery'] is False


def test_archive_filenames_and_versions_do_not_invent_a_year():
    assert len(ASSETS) == 32
    for asset in ASSETS:
        assert len(asset['sha256']) == 64
        assert asset['byte_size'] > 0 and asset['width'] > 0 and asset['height'] > 0
        assert '/' not in asset['source_filename'] and '\\' not in asset['source_filename']
        assert '2027' not in asset['route_variant']
