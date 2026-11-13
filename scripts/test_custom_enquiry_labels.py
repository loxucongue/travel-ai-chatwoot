"""Route-specific enquiry labels must not require selecting a standard tour."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('profile_labels', ROOT / 'backend/app/reception_v3/profile_labels.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
BEIJING = {'profile_labels': {'beijing_request_kind': {'urgent': ['急件'], 'custom': ['北京訂製']}}}
ROUTES = {'beijing': BEIJING, 'tibet': {}}


def test_custom_enquiry_without_selected_route_adds_label():
    row = SimpleNamespace(memory={'beijing_request_kind': 'urgent'}, controls={'route_variant': ''}, environment='playground')
    module.apply(None, row, {}, routes=ROUTES, updates=row.memory)
    assert row.controls == {'route_variant': '', 'evaluation_labels': ['急件']}


def test_historical_field_does_not_retag_unrelated_enquiry():
    assert module.labels_for_profile({'beijing_request_kind': 'urgent'}, {}, ROUTES, {'party_size': 4}) == []


def test_shared_field_requires_route_scope():
    routes = {'a': {'profile_labels': {'status': {'urgent': ['A']}}}, 'b': {'profile_labels': {'status': {'urgent': ['B']}}}}
    assert module.labels_for_profile({'status': 'urgent'}, {}, routes, {'status': 'urgent'}) == []
    assert module.labels_for_profile({'status': 'urgent'}, routes['a'], routes, {'status': 'urgent'}) == ['A']


def test_invalid_or_unknown_value_does_not_create_label():
    for value in (None, {}, ['urgent'], 'unknown'):
        assert module.labels_for_profile({}, {}, ROUTES, {'beijing_request_kind': value}) == []


def test_selected_route_and_unique_update_deduplicate():
    assert module.labels_for_profile({'beijing_request_kind': 'urgent'}, BEIJING, ROUTES, {'beijing_request_kind': 'urgent'}) == ['急件']
