"""Nov6 approved flow: introduce a requested route before collecting its month.

Run with the deployment's backend on PYTHONPATH. Keeps prices, assets, channel
switches and operator content; installs through the existing route registry.
"""
from copy import deepcopy
import json
from pathlib import Path

ASSET_DESCRIPTIONS = Path(__file__).resolve().parents[1] / 'data/knowledge/china2go/visual-assets-20261106.json'


def describe_materials(db):
    from sqlalchemy import select
    from app.models import MaterialAsset
    source = json.loads(ASSET_DESCRIPTIONS.read_text(encoding='utf8'))
    changed = []
    for asset in db.scalars(select(MaterialAsset).where(MaterialAsset.file_hash.in_(source['assets']))):
        metadata = {**asset.metadata_json, **source['assets'][asset.file_hash], 'narrative_version': source['version']}
        if metadata != asset.metadata_json:
            asset.metadata_json = metadata
            changed.append(asset.id)
    db.flush()
    return changed

GUIDANCE = ('2026-11-06接待順序：客戶明確要看本線、月份未定，先start_introduction介紹行程，'
            '不先問月份或等月份計時。介紹完成先回答新問題，再問尚未知的月份，之後才補未知人數。'
            '已知月份仍先核對適用季節；未知月份不保證季節住宿或先報最低價。'
            '只問單項問題或比較多線時先答，不等同要求重播整套介紹。')


def updated(package):
    result = deepcopy(package)
    if (result.get('intake') or {}).get('field') != 'departure_window':
        return result
    result['intake']['enabled'] = False
    if result.get('package_version') and not result['package_version'].endswith('.visual-20261106'):
        result['package_version'] += '.visual-20261106'
    text = result.get('ai_guidance', '')
    text = text.replace('先問月份，30秒未答就介紹，介紹後才問人數；', '')
    if GUIDANCE not in text:
        text = GUIDANCE + '\n' + text
    result['ai_guidance'] = text
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
    import json
    print(json.dumps({'changed': main()}))
