"""Explicit quote currencies; legacy CNY fields remain readable without conversion."""
import re


CURRENCY_MARKERS = {
    'CNY': r'(?:CNY|RMB|人民[币幣]|[¥￥])',
    'TWD': r'(?:TWD|NTD|NT\s*\$|新[台臺][币幣]|[台臺][币幣])',
}


def policy_currency(policy):
    currency = policy.get('currency', 'CNY')
    if currency not in CURRENCY_MARKERS:
        raise ValueError('quote_currency_unsupported')
    return currency


def money_value(policy, data, field):
    currency = policy_currency(policy)
    if data.get('currency', currency) != currency:
        raise ValueError('quote_currency_mismatch')
    legacy = field + '_cny'
    if legacy in data:
        if currency != 'CNY' or field in data:
            raise ValueError('quote_currency_fields_mixed')
        return data[legacy]
    return data.get(field)


def money_fields(policy, data, *fields):
    currency = policy_currency(policy)
    result = {'currency': currency}
    for field in fields:
        result[field] = money_value(policy, data, field)
        if currency == 'CNY':
            result[field + '_cny'] = result[field]
    return result


def currency_text_error(text, currency):
    """Recognize amounts, not statements such as 'we do not accept RMB'."""
    number = r'\d[\d,]*(?:\.\d+)?'
    for other, marker in CURRENCY_MARKERS.items():
        if other != currency and re.search(
                rf'{marker}\s*(?:[¥￥$]\s*)?{number}|{number}\s*{marker}', text, re.I):
            return f'本公司報價使用 {currency}；不能發送 {other} 金額或自行換匯，請引用已審核的本幣報價。'
    return None


def validate_template_currency(template, currency):
    rendered = template.format(amount='12345')
    if currency_text_error(rendered, currency):
        raise ValueError('quote_template_currency_mismatch')
    if currency == 'TWD' and not re.search(CURRENCY_MARKERS['TWD'], rendered, re.I):
        raise ValueError('quote_template_currency_required')


def validate_package_currency(package, currency):
    if currency not in CURRENCY_MARKERS or package.get('currency', currency) != currency:
        raise ValueError('route_currency_mismatch')
    for name in ('party_pricing', 'seasonal_pricing', 'departure_availability'):
        policy = package.get(name)
        if policy and (name != 'departure_availability' or policy.get('special_departures')):
            if policy_currency(policy) != currency:
                raise ValueError('route_currency_mismatch')
    # Facts, quote fallbacks and scheduled introduction text must agree too.
    def visit(value):
        if isinstance(value, dict):
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, str) and currency_text_error(value, currency):
            raise ValueError('route_currency_text_mismatch')
    if currency == 'TWD':
        for name in ('knowledge_facts', 'fixed_answers', 'content_groups', 'ai_guidance', 'large_party'):
            visit(package.get(name))
