"""Idempotent update of approved sales copy, preserving route-specific facts."""
from copy import deepcopy


def update_package(original):
    package = deepcopy(original)
    policy = package.get('seasonal_pricing')
    if policy:
        prices = {b['script_id']: b['price_cny'] for b in policy.get('bands', [])}
        prices.update({b['script_id']: b['price_cny'] for b in policy.get('special_departures', [])})
        for script in package.get('fixed_answers', []):
            if script['id'] not in prices:
                continue
            lines = script['answer_text'].splitlines()
            included = next((s for s in lines if s.startswith('包含：')), None)
            excluded = next((s for s in lines if s.startswith('不含：')), None)
            if not included or not excluded:
                raise ValueError('quote_inclusions_missing:' + package['route_variant'] + ':' + script['id'])
            dates = [s for s in lines if '出發：' in s]
            # Keep the published route's inclusion list, accommodation basis,
            # seasonal amount and dates; do not borrow another route's benefits.
            script['answer_text'] = '\n'.join([
                lines[0], f'拼團升級「4-6人小團」優惠價：人民幣¥ {prices[script["id"]]:,} /人（2人一間）',
                *dates, '', included, '', excluded])
    for group in package.get('content_groups', {}).values():
        text = group.get('approved_text', '')
        text = text.replace('我們專門做4–10人小團、全外賓不拼內賓、零購物的西藏行程！',
            '我們專門做全外賓不拼內賓、零購物的西藏小團，滿2人就發團，6人就滿團～')
        group['approved_text'] = text
    if package != original:
        package['package_version'] = '2026-10-27.audit-priority-v1'
    return package
