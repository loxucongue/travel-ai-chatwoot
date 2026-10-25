"""Add dated availability to peach products without changing prices or offers.

The pure updater can also be applied to backed-up runtime packages at deployment.
The CLI only updates source packages in this workspace, never a live database.
"""
from copy import deepcopy
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = ROOT/'data/knowledge/china2go/route-packages'


def update_package(package):
    updated = deepcopy(package)
    if updated.get('route_variant') in ('peach_9d_2027', 'peach_11d_2027'):
        updated['departure_availability'] = {
            'years': [2027], 'ranges': [['03-20', '04-10']], 'weekdays': [4, 5, 6, 0],
            'minimum_notice_days': 30,
            'private_departure_min_party_size': 4,
            'source_ref': 'Existing approved peach departure window and weekdays; business clarification 2026-10-14',
        }
    return updated


def main():
    for days in (9, 11):
        path = PACKAGE_ROOT/f'peach-{days}d-2027/route-package.json'
        original = json.loads(path.read_text(encoding='utf8'))
        path.write_text(json.dumps(update_package(original), ensure_ascii=False, indent=2)+'\n', encoding='utf8')
    print(json.dumps({'availability_configured': 2, 'prices_changed': False, 'production_changed': False}))


if __name__ == '__main__':
    main()
