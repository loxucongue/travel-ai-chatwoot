"""Pure, narrow package migration; no database or customer writes."""
from copy import deepcopy
import json
from pathlib import Path

POLICY_PATH = Path(__file__).resolve().parents[2] / 'data/knowledge/china2go/route-releases/20261108-seasonal-party/group-pricing.json'


def update_package(package):
    definition = json.loads(POLICY_PATH.read_text(encoding='utf8'))
    updated = deepcopy(package)
    if package.get('route_variant') == definition['route_variant']:
        updated['seasonal_pricing']['group_pricing'] = definition['group_pricing']
    return updated
