"""Narrow, repeatable content update; no intent rules or customer-state writes.

Apply to current packages, never replace them with a historical full snapshot.
The release runner must hold the production deployment lock and retain backups.
"""
from copy import deepcopy
import json
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / 'data/knowledge/china2go/introduction-copy-20261111.json'


def updated(package, spec=None):
    spec = spec or json.loads(SOURCE.read_text(encoding='utf8'))
    result = deepcopy(package)
    if result['route_variant'] not in spec['routes']:
        return result
    for path, text in spec['updates'].items():
        node = result['content_groups']
        keys = path.split('.')
        for key in keys[:-1]:
            node = node[key]
        if not isinstance(node[keys[-1]], str):
            raise ValueError(f'incompatible_content_field:{path}')
        node[keys[-1]] = text
    suffix = '.' + spec['version']
    if not result['package_version'].endswith(suffix):
        result['package_version'] += suffix
    return result


def main():
    from app.route_packages import ROUTE_PACKAGES, install_route_package
    changed = []
    for route, package in list(ROUTE_PACKAGES.items()):
        result = updated(package)
        if result != package:
            install_route_package(result)
            changed.append(route)
    return changed


if __name__ == '__main__':
    print(json.dumps({'changed': main()}))
