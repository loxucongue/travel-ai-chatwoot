"""Ensure deterministic date evidence never consumes a party size as a day."""
import importlib.util
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('date_precision', ROOT / 'backend/app/reception_v3/seasonal_quotes.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.mark.parametrize('text', [
    '2027年3月6位北京五天', '2027年3月6人', '2027年3月6個大人',
    '2027年3月6天', '2027年3月12位', '2027年3月6',
    '2027年3月6日或3月8日', '不要2027年3月6日',
    '2027-02-29出發', '2027年3月6日至8日',
])
def test_ambiguous_or_invalid_date_is_left_to_model(text):
    assert module.explicit_date_window(text) is None


@pytest.mark.parametrize('text', [
    '2027年3月6日6位', '2027年3月6號出發', '2027年3月6号出发',
    '2027-03-06出發', '2027/3/6出發',
])
def test_explicit_date_still_supported(text):
    assert module.explicit_date_window(text)['start'] == '2027-03-06'
