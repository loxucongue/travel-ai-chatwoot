"""Build a reviewable intake-only configuration candidate. Never connects to a DB.

python scripts/configure_small_fixes_20261024.py input.json candidate.json
The input must be the complete route_reception_config export.
"""
from copy import deepcopy
import json
from pathlib import Path
import sys


def updated(config):
    result = deepcopy(config)
    result.setdefault('intake', {})['wait_seconds'] = 90
    return result


if __name__ == '__main__':
    before = json.loads(Path(sys.argv[1]).read_text(encoding='utf8'))
    after = updated(before)
    destination = Path(sys.argv[2])
    if destination.resolve() == Path(sys.argv[1]).resolve():
        raise SystemExit('Use a separate candidate file; do not overwrite the export.')
    destination.write_text(json.dumps(after, ensure_ascii=False, indent=2), encoding='utf8')
    print(json.dumps({'applied': False, 'wait_seconds_before': before.get('intake', {}).get('wait_seconds'),
                      'wait_seconds_after': 90, 'candidate': str(destination), 'sessions_replayed': 0}))
