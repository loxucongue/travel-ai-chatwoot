"""Remove obsolete explanation restrictions; preserve published prices and flows."""
from copy import deepcopy

VERSION_SUFFIX = '.discount-explanation-20261103'


def update_package(original):
    package = deepcopy(original)
    if not package.get('party_pricing'):
        return package
    for fact in package.get('knowledge_facts', []):
        if fact['id'].endswith('.pricing_policy'):
            fact['text'] = fact['text'].replace(
                '不對外列折扣拆解或截止日。',
                '可依本線已配置的每人優惠簡短解釋價差，最終價已含優惠，不重複扣減；不展示早鳥或截止日。'
                '本規則僅作本線依據，其他線路須查其自己的報價政策。')
    for script in package.get('fixed_answers', []):
        if script['id'] == 'price':
            script['usage_note'] = script.get('usage_note', '').replace(
                '不展示早鳥、限時或折扣拆解。',
                '可解釋本線每人優惠，已包含在最終價；不展示早鳥或限時文案。')
    # Preserve ordering between shipped defaults and newer operator publications.
    if not package['package_version'].endswith(VERSION_SUFFIX):
        package['package_version'] += VERSION_SUFFIX
    return package
