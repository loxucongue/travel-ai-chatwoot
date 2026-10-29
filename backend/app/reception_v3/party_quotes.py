"""Compile approved head-count prices; the model selects, but cannot do arithmetic."""
def validate_party_pricing(policy):
    if not isinstance(policy, dict) or policy.get('currency') != 'CNY':
        raise ValueError('party_pricing_invalid')
    base = policy.get('base_price_cny')
    if type(base) is not int or base <= 0 or not policy.get('source_ref'):
        raise ValueError('party_pricing_base_invalid')
    discounts = policy.get('discounts_per_person', {})
    progression = policy.get('discount_progression')
    supplements = policy.get('private_supplements_per_person', {})
    if not isinstance(discounts, dict) or not isinstance(supplements, dict):
        raise ValueError('party_pricing_rates_invalid')
    if progression is not None:
        if (discounts or not isinstance(progression, dict)
                or any(type(progression.get(k)) is not int or not 1 <= progression[k] <= 10000
                       for k in ('starts_at', 'party_size_step', 'amount_step_cny'))
                or type(policy.get('auto_quote_party_size_max')) is not int
                or not 1 <= policy['auto_quote_party_size_max'] <= 100):
            raise ValueError('party_pricing_progression_invalid')
        if discount_per_person(policy, policy['auto_quote_party_size_max']) >= base:
            raise ValueError('party_pricing_rate_invalid')
    elif not discounts:
        raise ValueError('party_pricing_rates_invalid')
    for rates in (discounts, supplements):
        for count, value in rates.items():
            if (not isinstance(count, str) or not count.isdigit() or not 1 <= int(count) <= 100
                    or type(value) is not int or value < 0):
                raise ValueError('party_pricing_rate_invalid')
    if any(value >= base for value in discounts.values()):
        raise ValueError('party_pricing_rate_invalid')
    if progression is None and set(supplements)-set(discounts):
        raise ValueError('party_pricing_rate_invalid')
    if progression is not None and any(int(n) > policy['auto_quote_party_size_max'] for n in supplements):
        raise ValueError('party_pricing_rate_invalid')
    for key in ('shared_template', 'private_template'):
        template = policy.get(key, '')
        if not isinstance(template, str) or template.count('{amount}') != 1:
            raise ValueError('party_pricing_template_invalid')
        try:
            template.format(amount='1')
        except (KeyError, ValueError, IndexError) as exc:
            raise ValueError('party_pricing_template_invalid') from exc
    legacy = policy.get('legacy_offer_price_cny')
    if legacy is not None and (type(legacy) is not int or legacy <= 0):
        raise ValueError('party_pricing_legacy_invalid')


def discount_per_person(policy, count):
    """A reached tier applies until the next threshold; group fees stay separate."""
    if type(count) is not int or count < 1:
        raise ValueError('party_size_invalid')
    progression = policy.get('discount_progression')
    if progression is None:
        return policy['discounts_per_person'].get(str(count))
    steps = max(0, 1 + (count - progression['starts_at']) // progression['party_size_step'])
    return steps * progression['amount_step_cny']


def price_scripts(policy, *, legacy_ceiling=None):
    validate_party_pricing(policy)
    result = []
    counts = (range(1, policy['auto_quote_party_size_max'] + 1) if policy.get('discount_progression')
              else [int(n) for n in policy['discounts_per_person']])
    for group in ('shared', 'private'):
        for count in counts:
            discount = discount_per_person(policy, count)
            supplement = policy['private_supplements_per_person'].get(str(count))
            if group == 'private' and supplement is None:
                continue
            amount = policy['base_price_cny'] - discount + (supplement if group == 'private' else 0)
            # Existing, unexpired shared quotes are honoured as a ceiling, not
            # used as a second discounted base. New quotes create no deadline.
            if group == 'shared' and legacy_ceiling is not None:
                amount = min(amount, legacy_ceiling)
            result.append({'id':f'price_{group}_{count}', 'name':f'{count}位' + ('拼團' if group=='shared' else '獨團') + '最終每人價',
                           'answer_text':policy[group+'_template'].format(amount=f'{amount:,}'),
                           'asset_ids':[], 'status':'active', 'topics':['price'],
                           'party_size_min':int(count), 'party_size_max':int(count),
                           'price_cny':amount, 'quote_group_type':group,
                           'usage_note':'已按原價與明確人數計算。只引用符合本輪人數和團型的script_id，text留空；'
                                        '不再扣早鳥、不疊加舊活動、不展示截止日或計算分解。'})
    return result


def effective_profile(previous, changes):
    profile = {**previous, **changes}
    if changes.get('party_size') is not None:
        profile.pop('party_size_range', None)
    elif changes.get('party_size_range'):
        profile.pop('party_size', None)
    return profile


def pricing_evidence(bundle):
    """Expose comparisons from the same configured arithmetic as quote scripts."""
    result = {}
    for key, spec in bundle['routes'].items():
        policy = spec.get('party_pricing')
        if not policy:
            continue
        result[key] = {
            'source_ref': policy['source_ref'],
            'per_person_prices': [
                {'party_size': s['party_size_min'], 'group_type': s['quote_group_type'], 'price_cny': s['price_cny']}
                for s in price_scripts(policy)],
            'usage': '人数区间不等于价格不同。比较上表每人价，相同就说明同价，不以不存在的价差催问人数。'
                     '区间跨档才说明差别；正式最终报价仍按已确认人数和模板。'
                     '此表不代表日期可售。客户改了月份，先核对日期；该日期不可售就不报原线价格，也不为原线追问核价人数。',
        }
    return result


def quote_error(decision, bundle, previous, default_route=''):
    from app.reception_v3.seasonal_quotes import contains_trip_quote
    from app.reception_v3.route_choice import reply_route
    route = reply_route(decision, previous, default_route)
    spec = bundle['routes'].get(route, {})
    if not spec.get('party_pricing') or decision['action'] == 'handoff':
        return None
    pricing = [m for m in decision['messages'] if (m.get('script_id') or '').startswith('price')
               or contains_trip_quote(m.get('text', ''))]
    if not pricing:
        return None
    profile = effective_profile(previous, decision.get('profile', {}))
    count = profile.get('party_size')
    if type(count) is not int or count < 1 or profile.get('party_size_range'):
        return '最終每人價需要明確人數；先問本次要按幾位核價，不把人數區間下界當確定人數，不自行報金額。'
    group = profile.get('group_type', 'undecided')
    if group not in ('shared', 'private', 'undecided'):
        return '先確認拼團或獨團選擇，不猜團型核價。'
    group = 'shared' if group == 'undecided' else group
    expected = f'price_{group}_{count}'
    script = next((s for s in spec['scripts'] if s['id']==expected), None)
    if script is None:
        return '尚無此人數／團型的完整最終報價模板，不能推斷獨團加價或套用其他團型價格；需要實際報價時靜默交接核對。'
    if len(pricing) != 1 or any(m.get('script_id')!=expected or m.get('text') for m in pricing):
        return f'本輪核價只能引用{expected}，text留空，系統發送最終每人價與包含／不含；不可自行改寫金額或重複報價。'
    return None


def apply_price_scripts(spec, *, legacy_ceiling=None):
    spec['scripts'] = [s for s in spec['scripts'] if not s['id'].startswith('price')]
    if spec.get('party_pricing_enabled', True):
        spec['scripts'] += price_scripts(spec['party_pricing'], legacy_ceiling=legacy_ceiling)
    # Old static quote groups/facts must not compete with calculated templates.
    for key in ('price_reference', 'price_deferral'):
        if key in spec['groups']:
            spec['groups'][key]['text'] = '團費依確定人數及拼團／獨團選擇，使用對應price_*完整模板。'
    spec['quote_policy'] = None
