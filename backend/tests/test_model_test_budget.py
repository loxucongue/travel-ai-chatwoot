import json

import pytest

from app import model_metering


@pytest.mark.parametrize('limit,spent,reserve,allowed', [
    (19.5, 17.4, .7, True),
    (18, 17.4, .7, False),
    (19.5, 19.2, .4, False),
    (50, 19.8, .3, True),
    (50, 49.8, .3, False),
    (0, 0, .3, False),
    (float('inf'), 0, .3, False),
])
def test_reservations_respect_configured_and_authorized_total(tmp_path, monkeypatch, limit, spent, reserve, allowed):
    path = tmp_path / 'cost.json'
    previous = {'id': 'unknown-request', 'charged_or_reserved_cny': spent, 'usage_known': False}
    path.write_text(json.dumps({'limit_cny': limit, 'calls': [previous]}), encoding='utf8')
    monkeypatch.setattr(model_metering, '_ledger_path', lambda: path)
    record = {'id': 'next-request', 'charged_or_reserved_cny': reserve}
    if allowed:
        model_metering._update(record, reserve=True)
    else:
        with pytest.raises(RuntimeError, match='model_test_budget_exhausted'):
            model_metering._update(record, reserve=True)
    ledger = json.loads(path.read_text(encoding='utf8'))
    assert ledger['calls'][0] == previous
    assert len(ledger['calls']) == (2 if allowed else 1)
