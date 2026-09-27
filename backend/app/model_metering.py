"""Actual transport accounting; optional isolated-test budget, never live credentials."""
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from threading import RLock
import json
import os
import time
import uuid

calls = ContextVar('model_http_calls', default=None)
_lock = RLock()


def _ledger_path():
    value = os.environ.get('MODEL_TEST_COST_LEDGER')
    if not value:
        return None
    from app.config import settings
    if settings.app_profile != 'evaluation' or settings.outbound_enabled:
        raise RuntimeError('model_test_budget_requires_isolation')
    return Path(value)


def _update(record, *, reserve=False):
    path = _ledger_path()
    if path is None:
        return
    with _lock:
        ledger = json.loads(path.read_text(encoding='utf8')) if path.exists() else {'limit_cny': 18, 'calls': []}
        if reserve:
            spent = sum(r['charged_or_reserved_cny'] for r in ledger['calls'])
            if spent + record['charged_or_reserved_cny'] > min(18, float(ledger.get('limit_cny', 18))):
                raise RuntimeError('model_test_budget_exhausted')
            ledger['calls'].append(record.copy())
        else:
            ledger['calls'] = [record.copy() if r['id'] == record['id'] else r for r in ledger['calls']]
        ledger['charged_or_reserved_cny'] = round(sum(r['charged_or_reserved_cny'] for r in ledger['calls']), 8)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix('.pending')
        temp.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding='utf8')
        temp.replace(path)


def begin_request(payload):
    if _ledger_path() is not None and payload.get('model') not in {'deepseek-flash', 'deepseek-v4-flash'}:
        raise RuntimeError('model_test_requires_deepseek_flash')
    # Byte-level tokenization cannot exceed the serialized request's UTF-8
    # bytes for its visible text. Double that bound and add 64K framing room;
    # cap at the model's 1M input context. Never refund old unknown requests.
    input_bound = min(1000000, 2 * len(json.dumps(payload, ensure_ascii=False).encode('utf8')) + 65536)
    maximum = int(payload.get('max_tokens') or 384000)
    record = {'id': uuid.uuid4().hex, 'model': payload.get('model'), 'status': 'reserved',
              'input_token_upper_bound': input_bound,
              'charged_or_reserved_cny': (input_bound * 2 + maximum * 8) / 1000000,
              'input_tokens': None, 'output_tokens': None, 'duration_ms': None}
    _update(record, reserve=True)
    if calls.get() is not None:
        calls.get().append(record)
    return record, time.monotonic()


def end_request(handle, body=None, error=None):
    record, started = handle
    usage = (body or {}).get('usage') or {}
    record.update(duration_ms=round((time.monotonic()-started)*1000),
                  status='failed' if error else 'completed', error=type(error).__name__ if error else None)
    if isinstance(usage.get('prompt_tokens'), int) and isinstance(usage.get('completion_tokens'), int):
        inp, out = usage['prompt_tokens'], usage['completion_tokens']
        hit = min(inp, max(0, int(usage.get('prompt_cache_hit_tokens') or 0)))
        record.update(input_tokens=inp, output_tokens=out, cached_input_tokens=hit,
                      charged_or_reserved_cny=((inp-hit)*2 + hit*.04 + out*8)/1000000,
                      usage_known=True)
    else:
        record['usage_known'] = False
    _update(record)


def metrics(records):
    return {'model_http_request_count': len(records), 'model_http_calls': records,
            'model_input_tokens': sum(r['input_tokens'] or 0 for r in records),
            'model_output_tokens': sum(r['output_tokens'] or 0 for r in records),
            'model_usage_unknown_calls': sum(not r.get('usage_known') for r in records)}


def merge_metrics(previous, current):
    records = {r['id']: r for trace in (previous, current)
               for r in trace.get('model_http_calls', []) if r.get('id')}
    return metrics(list(records.values()))


def measure_turn(function):
    @wraps(function)
    def measured(*args, **kwargs):
        records = []
        token = calls.set(records)
        try:
            decision, logs, digest, trace = function(*args, **kwargs)
            return decision, logs, digest, {**trace, **metrics(records)}
        except Exception as exc:
            exc.model_metrics = metrics(records)
            raise
        finally:
            calls.reset(token)
    return measured
