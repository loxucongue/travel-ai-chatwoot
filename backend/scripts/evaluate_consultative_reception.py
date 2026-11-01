"""Reproducible model-led reception comparison, with no customer/channel writes.

Example (credentials supplied by the normal environment):
python scripts/evaluate_consultative_reception.py --variant consultative \
  --thinking-effort high --cost-ceiling-cny 98 --snapshot snapshot.json \
  --cases tests/fixtures/consultative_holdout_20261101.json \
  --ledger cost.json --output output/trial

Use --variant baseline with the same snapshot/cases for the control. This wrapper
does not grant budget or change its ledger limit. The existing runner additionally
blocks HTTP to everything except the model endpoint and simulates all sends.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import runpy
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--variant', choices=('baseline', 'consultative'), required=True)
    parser.add_argument('--thinking-effort', choices=('low', 'high'), default='low')
    parser.add_argument('--cost-ceiling-cny', type=float, required=True,
                        help='Absolute charged/reserved total, not an additional budget grant')
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--cases', type=Path, required=True)
    parser.add_argument('--ledger', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--full-journeys', action='store_true')
    args = parser.parse_args()
    for key in ('snapshot', 'cases', 'ledger', 'output'):
        setattr(args, key, getattr(args, key).resolve())
    ledger = json.loads(args.ledger.read_text(encoding='utf-8'))
    if not 0 < args.cost_ceiling_cny <= ledger['limit_cny']:
        raise ValueError('ceiling_must_fit_existing_authorization')
    os.environ.update(APP_PROFILE='evaluation', DATABASE_URL='sqlite://', OUTBOUND_MODE='disabled',
        CHATWOOT_WRITE_ENABLED='false', LIVE_SOP_ENABLED='false', RELAY_BASE_URL='', RELAY_API_TOKEN='',
        BUSINESS_PROFILE='china2go', MODEL_TEST_COST_LEDGER=str(args.ledger),
        RECEPTION_ADVISOR_EXPERIENCE_ENABLED='false', RECEPTION_ROUTE_UNDERSTANDING_ENABLED='true',
        RECEPTION_CONSULTATIVE_PILOT_ENABLED=str(args.variant == 'consultative').lower(),
        RECEPTION_CONSULTATIVE_REASONING_EFFORT=args.thinking_effort)
    backend = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(backend))
    from app import model_metering
    original = model_metering._update

    def update(record, *, reserve=False):
        if reserve:
            current = json.loads(args.ledger.read_text(encoding='utf-8'))
            used = sum(c['charged_or_reserved_cny'] for c in current['calls'])
            if used + record['charged_or_reserved_cny'] > args.cost_ceiling_cny:
                raise RuntimeError('experiment_cost_ceiling_exhausted')
        return original(record, reserve=reserve)

    sources = [backend/'app/reception_v3'/n for n in (
        'consultative.py', 'consultative_brief.md', 'runtime.py', 'skills.py', 'prompt.py', 'route_understanding.py')]
    sources += [backend/'app/config.py', Path(__file__)]
    fingerprint = lambda: {str(p.relative_to(backend)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
    before = fingerprint()
    args.output.mkdir(parents=True, exist_ok=True)
    manifest = {'variant': args.variant, 'thinking_effort': args.thinking_effort,
        'snapshot_sha256': hashlib.sha256(args.snapshot.read_bytes()).hexdigest(),
        'cases_sha256': hashlib.sha256(args.cases.read_bytes()).hexdigest(), 'sources': before,
        'start_charged_or_reserved_cny': sum(c['charged_or_reserved_cny'] for c in ledger['calls']),
        'cost_ceiling_cny': args.cost_ceiling_cny}
    (args.output/'consultative-manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    model_metering._update = update
    argv = sys.argv
    sys.argv = ['evaluate', '--snapshot', str(args.snapshot), '--cases', str(args.cases),
                '--output', str(args.output), '--ledger', str(args.ledger), '--workers', '1']
    if args.full_journeys:
        sys.argv.append('--full-journeys')
    try:
        runpy.run_path(str(backend/'scripts/evaluate_reception_journeys.py'), run_name='__main__')
    finally:
        model_metering._update = original
        sys.argv = argv
        if before != fingerprint():
            raise RuntimeError('consultative_sources_changed_during_test')


if __name__ == '__main__':
    main()
