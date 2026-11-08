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
                           'usage_note':'已按配置基準與明確人數計算，對客稱優惠價，不稱原價。只引用符合本輪人數和團型的script_id，text留空；'
                                        '不展示早鳥或截止日，不疊加舊活動。可另用text依pricing_evidence簡短說明'
                                        '各人數的每人優惠，最終價已含適用優惠，不再扣一次。'})
    return result


def effective_profile(previous, changes):
    profile = {**previous, **changes}
    if changes.get('party_size') is not None:
        profile.pop('party_size_range', None)
    elif changes.get('party_size_range'):
        profile.pop('party_size', None)
    return profile


def shared_reference(spec):
    """Use an existing baseline shared quote without inventing a customer count.

    The personalized script preserves any valid prior lower price. No new
    pricing policy or customer intent is inferred here.
    """
    return min((s for s in spec['scripts'] if s.get('status') == 'active'
                and s.get('quote_group_type') == 'shared' and s.get('price_cny')
                and discount_per_person(spec['party_pricing'], s['party_size_min']) == 0),
               key=lambda s: s['party_size_min'], default=None)


def pricing_evidence(bundle):
    """Expose comparisons from the same configured arithmetic as quote scripts."""
    result = {}
    for key, spec in bundle['routes'].items():
        seasonal = spec.get('seasonal_pricing') or {}
        if seasonal.get('group_pricing'):
            from app.reception_v3.seasonal_quotes import group_quote
            policy = seasonal['group_pricing']
            bands = []
            for band in seasonal['bands']:
                prices = []
                for n in policy['discounts_per_person']:
                    quote = group_quote({'price_cny': band['price_cny'], 'status': 'quoted'},
                                        policy, int(n), 'private' if int(n) >= policy['private_from'] else 'undecided')
                    prices.extend({k: option[k] for k in (
                        'party_size', 'group_type', 'price_cny', 'total_price_cny',
                        'discount_per_person_cny', 'private_supplement_per_person_cny')}
                        for option in quote.get('options', []))
                bands.append({'ranges': band['ranges'], 'per_person_prices': prices})
            result[key] = {'source_ref': policy['source_ref'], 'seasonal_prices': bands,
                           'usage': '本線季節與人數團型最終價，已扣多人優惠，不套其他線路價格。'
                                    '先用get_departure_quote核對日期，跨季分別核價；以text報本線最終價，'
                                    '不使用季節基準價模板，不再扣優惠。六人僅提供獨團，不另造拼團方案。'}
            continue
        policy = spec.get('party_pricing')
        if not policy:
            continue
        prices = []
        for script in spec['scripts']:
            if script.get('status') != 'active' or not script.get('price_cny') or not script.get('quote_group_type'):
                continue
            count, group = script['party_size_min'], script['quote_group_type']
            discount = discount_per_person(policy, count)
            supplement = policy['private_supplements_per_person'][str(count)] if group == 'private' else 0
            standard = policy['base_price_cny'] - discount + supplement
            prices.append({
                'script_id': script['id'],
                'party_size': count, 'group_type': group, 'price_cny': script['price_cny'],
                'discount_per_person_cny': discount, 'private_supplement_per_person_cny': supplement,
                'standard_price_cny': standard, 'honors_prior_lower_quote': script['price_cny'] < standard,
            })
        reference = shared_reference(spec)
        result[key] = {
            'reference_script_id': reference['id'] if reference else None,
            'source_ref': policy['source_ref'],
            'base_price_cny': policy['base_price_cny'],
            'per_person_prices': prices,
            'usage': '人数尚未确定但客户先问预算时，可用reference_script_id先给拼团参考优惠价，text留空；'
                     '它只是价格来源，不代表客户单人出行或已确定任何人数，不修改profile人数/团型，也不因此附单人房差。'
                     '需要时简短说明本线对应人数优惠，让客人先评估，不要求先确定人数才报价。'
                     '客户明确比较多个方案仍用quote_options；日期、团型、价格依据和历史承诺继续核对。'
                     '人数区间不等于价格不同。比较上表每人价，相同就说明同价，不以不存在的价差催问人数。'
                     '客户比较多种人数或假设单个人数方案时，用messages[].quote_options列出相应price_*模板ID（单个假设可只列一个），'
                     '系统将最终每人价相同的人数组合并为一行，不同价分别展示，共用包含/不含说明，无须先选定人数。'
                     '比较方案不是报名人数，不据此更新profile.party_size。单一确定人数仍用script_id。'
                     '比较的最终每人价不同时，另用一条text自然说明适用的discount_per_person_cny，这是每人减额，'
                     '不是整单减额；最终价已含优惠，不再扣一次。客户只问优惠规则就解释规则，不必重发完整报价。'
                     '解释按本轮表格，不按固定例句背金额。对客称优惠价，不称原价；base_price_cny是计算依据，不是对客价格名称。'
                     '无价差时只说明这些人数都是上述优惠价，不重复金额、不解释折扣有无或能否再减，也不主动扩展其他人数档位。'
                     '历史已报过且仍适用的同价，直接text承接上述优惠价，不重发quote_options或完整模板。不扩大到其他线路。'
                     '独团加价private_supplement_per_person_cny与人数优惠分开，不能把减额当净价差。'
                     'honors_prior_lower_quote为true时沿用先前较低报价，不把它说成按本次人数折扣算出的价格，'
                     '也不在先前报价上再扣人数优惠，不展示旧活动名称或期限。'
                     '此表不代表日期可售。客户改了月份，先核对日期；该日期不可售就不报原线价格，也不为原线追问核价人数。',
        }
    return result


def quote_error(decision, bundle, previous, default_route=''):
    from app.reception_v3.seasonal_quotes import contains_trip_quote
    from app.reception_v3.route_choice import reply_route
    route = reply_route(decision, previous, default_route)
    spec = bundle['routes'].get(route, {})
    comparisons = [m for m in decision['messages'] if m.get('quote_options')]
    if comparisons and not spec.get('party_pricing'):
        return '此線沒有按人數核價模板，不使用quote_options；沿用此線日期核價工具及已核對的報價。'
    if not spec.get('party_pricing') or decision['action'] == 'handoff':
        return None
    pricing = [m for m in decision['messages'] if (m.get('script_id') or '').startswith('price')
               or contains_trip_quote(m.get('text', '')) or m.get('quote_options')]
    if not pricing:
        return None
    profile = effective_profile(previous, decision.get('profile', {}))
    if comparisons:
        if len(comparisons) != 1:
            return '假設或比較人數請集中在一條quote_options，補充解釋另用text並以對應script_id標記金額來源。'
        message = comparisons[0]
        ids = message['quote_options']
        if (len(ids) < 1 or len(ids) != len(set(ids)) or message.get('text')
                or message.get('script_id') or message.get('asset_keys') or message.get('material_rule_id')):
            return 'quote_options需至少一個人數模板ID且不可重複；本條text、script_id及素材留空，補充說明另放一條。'
        scripts = {s['id']: s for s in spec['scripts'] if s.get('status') == 'active' and s.get('price_cny')}
        if any(key not in scripts or not scripts[key].get('quote_group_type') for key in ids):
            available = [key for key, value in scripts.items() if value.get('quote_group_type')]
            return ('quote_options引用了不存在或停用的ID，請按pricing_evidence中對應方案的script_id修正；'
                    '不要使用資料路徑或改寫客戶的假設人數為已確定人數。可用ID：' + ', '.join(available))
        groups = {scripts[key]['quote_group_type'] for key in ids}
        group = profile.get('group_type', 'undecided')
        if len(groups) != 1 or groups != {group if group in ('shared', 'private') else 'shared'}:
            return '本次比較需使用同一團型；未選團型先提供拼團參考價，不套用其他團型或猜獨團加價。'
        explanation_ids = [key for key, s in scripts.items() if s.get('quote_group_type') in groups]
        for extra in pricing:
            if extra is not message and not _validated_explanation(extra, spec, explanation_ids):
                return '完整比較報價只發一次；補充解釋可引用本線同團型已有依據的價格，不另發整份報價或猜配置以外的金額。'
        return None
    count = profile.get('party_size')
    if count is None or profile.get('party_size_range'):
        reference = shared_reference(spec)
        if not reference or profile.get('group_type', 'undecided') not in ('shared', 'undecided'):
            return '本次沒有適用的拼團參考價；按已知團型提供可核對方案，不把拼團參考價當包團報價。'
        expected = reference['id']
        full_quotes = [m for m in pricing if not m.get('text')]
        if (len(full_quotes) > 1 or any(m.get('script_id') != expected for m in full_quotes)
                or any(not _validated_explanation(m, spec, [expected]) for m in pricing if m.get('text'))):
            return f'人數未定可先用{expected}的拼團參考優惠價，不寫成已確定人數；比較不同人數用quote_options，不能任選較低檔位或編造金額。'
        return None
    if type(count) is not int or count < 1:
        return '人數資料無效，不用它核價；保留客戶實際表達，未知人數可用已配置拼團參考價。'
    group = profile.get('group_type', 'undecided')
    if group not in ('shared', 'private', 'undecided'):
        return '先確認拼團或獨團選擇，不猜團型核價。'
    group = 'shared' if group == 'undecided' else group
    expected = f'price_{group}_{count}'
    script = next((s for s in spec['scripts'] if s['id']==expected), None)
    if script is None:
        return '尚無此人數／團型的完整最終報價模板，不能推斷獨團加價或套用其他團型價格；需要實際報價時靜默交接核對。'
    full_quotes = [m for m in pricing if not m.get('text')]
    if (len(full_quotes) > 1 or any(m.get('script_id') != expected for m in full_quotes)
            or any(not _validated_explanation(m, spec, [expected]) for m in pricing if m.get('text'))):
        return f'上一候選含本輪方案以外的金額或重複模板，尚未發送。保持客戶問題範圍，只修正沒有依據的金額。只問已含優惠或能否再減，直接用text解釋本方案已核對價格即可，不追加其他人數、不重發包含／不含；可引用{expected}作來源。客戶要完整新報價時才引用此模板並把text留空。'
    return None


def _validated_explanation(message, spec, ids):
    """Verify the model's price source without classifying its wording."""
    from app.reception_v3.seasonal_quotes import trip_quote_amounts
    if (not message.get('text') or (message.get('script_id') and message['script_id'] not in ids)
            or message.get('quote_options') or message.get('asset_keys') or message.get('material_rule_id')):
        return False
    scripts = {s['id']: s for s in spec['scripts'] if s.get('status') == 'active' and s.get('price_cny')}
    if any(key not in scripts for key in ids):
        return False
    amounts = {scripts[key]['price_cny'] for key in ids}
    amounts.add(spec['party_pricing']['base_price_cny'])
    return set(trip_quote_amounts(message['text'])) <= amounts


def comparison_text(spec, ids):
    """Render verified alternatives, grouping equal effective per-person prices.

    Preserve the configured price line's wording and every surrounding term;
    amounts come from personalized scripts, including any valid legacy ceiling.
    The caller validates the IDs and common group before rendering.
    """
    scripts = {s['id']: s for s in spec['scripts']}
    selected = [scripts[key] for key in ids]
    template = spec['party_pricing'][selected[0]['quote_group_type'] + '_template']
    before, after = template.split('{amount}')
    header, _, price_prefix = before.rpartition('\n')
    price_suffix, _, footer = after.partition('\n')
    by_price = {}
    for script in selected:
        by_price.setdefault(script['price_cny'], []).append(script['party_size_min'])
    lines = [f"{'／'.join(f'{count}位' for count in counts)}報名：{price_prefix}{amount:,}{price_suffix}"
             for amount, counts in by_price.items()]
    return '\n'.join(part for part in (header, '\n'.join(lines), footer) if part)


def apply_price_scripts(spec, *, legacy_ceiling=None):
    spec['scripts'] = [s for s in spec['scripts'] if not s['id'].startswith('price')]
    if spec.get('party_pricing_enabled', True):
        spec['scripts'] += price_scripts(spec['party_pricing'], legacy_ceiling=legacy_ceiling)
    # Old static quote groups/facts must not compete with calculated templates.
    for key in ('price_reference', 'price_deferral'):
        if key in spec['groups']:
            spec['groups'][key]['text'] = '人數未定可先用pricing_evidence.reference_script_id提供拼團參考優惠價；比較用quote_options，確認人數後用對應price_*模板。'
    spec['quote_policy'] = None
