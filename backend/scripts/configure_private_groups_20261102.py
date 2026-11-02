"""Extend the existing group introduction to explicit private intent, preserving route content."""
from copy import deepcopy


def update_package(package):
    result = deepcopy(package)
    policy = result.get('large_party')
    if not policy or not policy.get('enabled'):
        return result
    policy.update(
        include_explicit_private=True,
        source_ref='https://china2go.com/8425-2/；2026-11-01 21:14麗麗确认明确包团也沿用此介绍后交接流程，不限7人',
        opening_text='您們{party_description}的行程，我們會按獨立一團的方向確認～',
        completed_text='您們{party_description}的行程，我們會按獨立一團的方向確認～剛剛看的是基本路線，日期、天數和想去的景點可以再和顧問討論。',
    )
    suffix='+private-groups-20261102'
    if not result['package_version'].endswith(suffix):
        result['package_version'] += suffix
    return result
