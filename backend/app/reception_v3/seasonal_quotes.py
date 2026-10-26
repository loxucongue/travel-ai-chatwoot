"""Check business departure windows without guessing dates or discount amounts."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
import re
import calendar


def seasonal_group(group, profile):
    """Use dated accommodation text; undated/cross-season trips get no room photos."""
    override = group.get('seasonal_override')
    if not override:
        return group
    window = str(profile.get('departure_window') or '')
    months = {int(m) for m in re.findall(r'(\d{1,2})\s*月', window)}
    months.update(int(m) for m in re.findall(r'(?:20\d{2}[-/.])?(\d{1,2})[-/.]\d{1,2}', window))
    for i, token in enumerate(('一','二','三','四','五','六','七','八','九','十','十一','十二'), 1):
        if re.search(r'(?<![一二三四五六七八九十])'+token+'月', window):
            months.add(i)
    if re.search(r'春[節节]|過年|过年', window):
        months.add(2)
    if re.search(r'冬[天季]|冬遊|冬游', window):
        months.update((12, 1, 2))
    if not months and not window:
        evidence = profile.get('_departure_check') or {}
        for key in ('start', 'end'):
            try:
                months.add(date.fromisoformat(evidence[key]).month)
            except (KeyError, ValueError, TypeError):
                pass
    winter = set(override['months'])
    if months and months <= winter:
        return {**group, 'text': override['text'], 'assets': [], 'delivery_mode': 'text_only'}
    if not months or months & winter:
        return {**group, 'text': override['undated_text'], 'assets': [], 'delivery_mode': 'text_only'}
    return group


def mentions_departure_time(text):
    """Pause a running introduction while a newly supplied travel time is checked."""
    return bool(re.search(r'(?:\d{1,2}|[一二三四五六七八九十]{1,3})\s*月|\d{1,2}[/.-]\d{1,2}|春[節节]|過年|过年|冬[天季]', text))


def explicit_date_window(text):
    """Only one unambiguous, fully dated customer proposal may bypass inference."""
    matches=list(re.finditer(r'(?<!\d)(20\d{2})(?:年|[-/])(\d{1,2})(?:月|[-/])(\d{1,2})(?:日|號|号)?(?!\d)',text))
    if len(matches)!=1:
        return None
    match=matches[0]
    if re.search(r'不(?:要|是|去|能)|取消|別|别',text[max(0,match.start()-10):match.start()]):
        return None
    # A second month/day without its own year is still a comparison/range.
    remaining=text[:match.start()]+text[match.end():]
    if re.search(r'\d{1,2}(?:月|[-/])\d{1,2}|(?:至|到|或|還是|还是)\s*\d{1,2}[日號号]',remaining):
        return None
    try:
        value=date(*(int(x) for x in match.groups())).isoformat()
    except ValueError:
        return None
    return {'start':value,'end':value,'customer_month':value,'year_inferred':False}


def reply_calendar_error(decision, context, checks):
    """Validate calendar claims against checked dates, not model arithmetic."""
    if decision['action']=='handoff':
        return None
    target=(decision.get('profile',{}).get('discussion_route') if decision.get('route_choice')=='recommend'
            else decision.get('route_variant') or context.get('route_variant'))
    evidence=checks.get(target) or {}
    if not evidence:
        for key in ('_offered_departure_check','_departure_check'):
            candidate=context.get('profile',{}).get(key) or {}
            if candidate.get('route')==target:
                evidence=candidate;break
    text='\n'.join(m['text'] for m in decision['messages'])
    year=str(evidence.get('start',''))[:4]
    pattern=r'(?:(20\d{2})(?:年|[-/]))?(\d{1,2})(?:月|[/.-])(\d{1,2})日?\s*(?:是|剛好是|刚好是|正好是|為|为|剛好|刚好)?\s*[（(]?(?:星期|週|周)([一二三四五六日天])'
    for match in re.finditer(pattern,text):
        stated_year=match[1] or year
        if not stated_year.isdigit() or len(stated_year)!=4:
            continue
        try:
            day=date(int(stated_year),int(match[2]),int(match[3]))
        except ValueError:
            return '回覆日期不存在，請按客人原話核對，不發送無效日期。'
        weekday='一二三四五六日'[day.weekday()]
        if match[4].replace('天','日')!=weekday:
            return f'日曆校驗：{day.isoformat()}是週{weekday}，不是週{match[4]}。更正或省略星期，不改客人的日期。'
    policy=context['skills']['routes'].get(target,{}).get('departure_availability') or {}
    try:
        start,end=date.fromisoformat(evidence['start']),date.fromisoformat(evidence['end'])
    except (KeyError,ValueError,TypeError):
        return None
    if start == end:
        # Resolve a date pronoun using the verified inquiry, not an intervening
        # advertised range's endpoint.
        weekdays = re.findall(r'(?:這天|这天|當天|当天|該日|该日|那天)\s*(?:是|為|为|剛好是|刚好是|正好是)\s*(?:星期|週|周)([一二三四五六日天])', text)
        expected = '一二三四五六日'[start.weekday()]
        if any(day.replace('天','日') != expected for day in weekdays):
            return f'日曆校驗：所詢日期{start.isoformat()}是週{expected}。更正或省略「這天」的星期，不改客人的日期。'
    if (start!=end and policy.get('ranges') and
            (not _in_ranges(start,policy['ranges']) or not _in_ranges(end,policy['ranges'])) and
            re.search(r'(?:時間|时间|日期).{0,10}(?:對得上|对得上|吻合|符合|合適|合适|沒問題|没问题|剛好在|刚好在)',text)):
        return '客人只給月份/區間，尚未確認具體出發日；該區間不全在公布團期內。只說明可出發日期，不能說客人時間已對上或已符合。'
    return None


def month_answer_window(text, now, known_window=''):
    """Resolve short affirmative month answers; questions/negatives/ranges stay with the model."""
    match = re.fullmatch(r'\s*(?:希望|想要|想|預計|预计)?\s*(?:(20\d{2})年|(?P<relative>明年|今年))?\s*(?P<month>1[0-2]|0?[1-9]|十一|十二|[一二三四五六七八九十])\s*月(?:份)?[。！!～~\s]*', text)
    if not match:
        return None
    token = match['month']
    month = int(token) if token.isdigit() else ['一','二','三','四','五','六','七','八','九','十','十一','十二'].index(token)+1
    today = datetime.fromisoformat(now.replace('Z', '+00:00')).astimezone(ZoneInfo('Asia/Taipei')).date()
    year = int(match[1]) if match[1] else today.year + (match['relative'] == '明年' or (not match['relative'] and month < today.month))
    # Preserve a previously stated year. Do not treat a tool's inferred year
    # as customer confirmation, or pick a year from a multi-year comparison.
    known_years = set(re.findall(r'(20\d{2})(?=年|[-/]\d{1,2})', str(known_window or '')))
    if (not match[1] and not match['relative'] and not known_years
            and re.search(r'今年|明年|後年|后年', str(known_window or ''))):
        # Relative history needs its original message time; the raw slot does
        # not carry that anchor. Let the full conversation resolve it instead
        # of contradicting it with this turn's default next-future month.
        return None
    if not match[1] and not match['relative'] and known_years:
        if len(known_years) != 1:
            return None
        year = int(next(iter(known_years)))
    begin = date(year, month, 1)
    finish = date(year, month, calendar.monthrange(year, month)[1])
    # A month-only answer includes the remaining days of this month. Do not
    # call it a past-year mistake just because the first day has passed.
    # A wholly past, explicitly stated month stays past and needs clarification.
    if begin < today <= finish:
        begin = today
    return {'start': begin.isoformat(), 'end': finish.isoformat(),
            'customer_month': (f'{year}年{month}月' if known_years and not match[1] and not match['relative'] else text.strip()),
            'year_inferred': not bool(match[1] or match['relative'] or known_years)}


def validate_availability(policy):
    if (not isinstance(policy, dict) or not policy.get('years') or not policy.get('ranges')
            or not policy.get('weekdays')):
        raise ValueError('departure_availability_invalid')
    if any(type(y) is not int or not 2000 <= y <= 2100 for y in policy['years']):
        raise ValueError('departure_availability_year_invalid')
    if any(type(w) is not int or not 0 <= w <= 6 for w in policy['weekdays']):
        raise ValueError('departure_availability_weekday_invalid')
    if type(policy.get('minimum_notice_days')) is not int or not 1 <= policy['minimum_notice_days'] <= 365:
        raise ValueError('departure_availability_notice_invalid')
    _validate_private_departure(policy)
    _validate_departure_overrides(policy)
    for pair in policy['ranges']:
        if not isinstance(pair, list) or len(pair) != 2:
            raise ValueError('departure_availability_range_invalid')
        begin, end = [date.fromisoformat('2028-' + x) for x in pair]
        if begin > end:
            raise ValueError('departure_availability_range_order')


def _in_ranges(day, ranges):
    return any(lo <= day.strftime('%m-%d') <= hi for lo, hi in ranges)


def _validate_departure_overrides(policy):
    for begin, end in policy.get('operating_periods', []):
        if date.fromisoformat(begin) > date.fromisoformat(end):
            raise ValueError('operating_period_order')
    seen = set()
    for item in policy.get('special_departures', []):
        day = date.fromisoformat(item['date'])
        if (day.isoformat() in seen or day.year not in policy['years']
                or type(item.get('price_cny')) is not int or item['price_cny'] <= 0
                or not str(item.get('script_id', '')).startswith('price_')
                or not item.get('source_ref') or item.get('group_type') != 'shared'):
            raise ValueError('special_departure_invalid')
        seen.add(day.isoformat())


def _within_operating_period(day, policy):
    periods = policy.get('operating_periods')
    return not periods or any(lo <= day.isoformat() <= hi for lo, hi in periods)


def _special_departure(policy, day):
    return next((item for item in policy.get('special_departures', []) if item['date'] == day.isoformat()), None)


def _validate_private_departure(policy):
    minimum = policy.get('private_departure_min_party_size')
    if minimum is not None and (type(minimum) is not int or minimum < 1):
        raise ValueError('private_departure_min_party_size_invalid')


def _can_choose_date(policy, party_size, group_type):
    minimum = policy.get('private_departure_min_party_size')
    return (type(minimum) is int and type(party_size) is int and party_size >= minimum
            and group_type != 'shared')


def _non_scheduled_result(spec, days, today, party_size, group_type):
    policy = spec.get('seasonal_pricing') or spec.get('departure_availability')
    if _can_choose_date(policy, party_size, group_type):
        return {'status': 'private_departure', 'departure_examples': [d.isoformat() for d in days[:3]],
                'minimum_party_size': policy['private_departure_min_party_size'],
                'reason': '已達獨團自選日期的人數門檻，可選所詢日期，不受拼團每週定班限制；不是名額或訂位確認。',
                'guidance': '此結果是日期可用性依據，不是每輪都要重述獨團條件的話術。首次被問能否自選日期時才說明；'
                            '已解釋過而本輪只補月份、原因或收尾，不再複述人數門檻與自選日期。客人明確重問可針對問題再說明。'
                            '尚未選獨團且有必要時可確認意願，不要求改到定班日、不索取聯絡方式。'
                            '未問價不報價；若問這個自選日期的實際獨團總報價，靜默交接核對，不能直接套拼團模板。'
                            '4位獨團每人加400元的已知條件仍可單獨回答；優惠叠加與其他未確認費用不猜。'}
    if policy.get('private_departure_min_party_size') and party_size is None and group_type != 'shared':
        return {'status': 'ask_date', 'reason_code': 'party_size_required',
                'reason': '所問日期不是拼團定班日，但人數未知；先問預計幾位。4位起可獨團自選日期，不能先要求改到定班日。'}
    suggestions = _available_departures(spec, [days[0]+timedelta(days=i) for i in range(1, 8)], today)
    return {'status': 'ask_date', 'reason': '所問日期不是拼團定班日；不足4位或明確選拼團，需按本線定班日期安排。',
            'scheduled_weekdays': policy.get('weekdays', [policy.get('weekday')]),
            'suggested_departures': suggestions[:3]}


def _available_departures(spec, days, today, party_size=None, group_type='undecided'):
    policy = spec.get('seasonal_pricing') or spec.get('departure_availability')
    if not policy:
        return []  # Missing availability is not evidence of a saleable product.
    result = []
    for day in days:
        if day.year not in policy['years'] or (day-today).days < policy['minimum_notice_days']:
            continue
        if not _within_operating_period(day, policy):
            continue
        if _special_departure(policy, day) and group_type != 'private' and (party_size is None or party_size <= 6):
            result.append(day.isoformat())
            continue
        if day.month in policy.get('advisor_months', []) + policy.get('review_months', []):
            continue
        if _in_ranges(day, policy.get('holiday_ranges', []) + policy.get('unavailable_ranges', [])):
            continue
        if spec.get('seasonal_pricing'):
            allowed = ((day.weekday() == policy['weekday'] or _can_choose_date(policy, party_size, group_type)) and
                       sum(_in_ranges(day, b['ranges']) for b in policy['bands']) == 1)
        else:
            allowed = (day.weekday() in policy['weekdays'] or _can_choose_date(policy, party_size, group_type)) and _in_ranges(day, policy['ranges'])
        if allowed:
            result.append(day.isoformat())
    return result


def _unavailable_result(bundle, route, days, today, reason, code, party_size=None, group_type='undecided'):
    alternatives = []
    for key, spec in bundle['routes'].items():
        if key == route:
            continue
        departures = _available_departures(spec, days, today, party_size, group_type)
        if departures:
            alternatives.append({'route_variant': key, 'name': spec.get('name', key),
                                 'departure_examples': departures[:3]})
    result = {'status': 'unavailable', 'reason_code': code, 'reason': reason,
            'alternatives': alternatives,
            'next_action': 'offer_alternatives' if alternatives else 'offer_reopening_notification',
            'guidance': '只推薦返回且符合客人天數/景點偏好的線路，日期例子不是庫存保證；先問意願，不自動改線。'
                        '沒有合適選擇時，可在客人主動詢問的本輪輕邀一種聯絡方式，方便開放後由我們聯絡。'
                        '已留資、拒絕留資、上一邀請未回或留資關閉時不重複索取；不在沉默事件索取。'
                        '僅邀請時reply，真正留資或要求真人才靜默handoff，內部登記開放通知意願。'
                        '不保證尚未公布的開放日期，不聲稱已登記或會自動通知。'}
    recommendation = (bundle['routes'][route].get('seasonal_pricing') or {}).get('winter_recommendation')
    if recommendation and not alternatives and all(d.month in recommendation['months'] for d in days):
        # Approved advice is distinct from a bookable route with dates/prices.
        # This also covers Dec -> Feb spanning an unpublished calendar year.
        result.update(next_action='offer_winter_recommendation', winter_recommendation=recommendation,
                      guidance='先說明冬季絨布旅館關閉、改住山下珠峰小鎮，推薦季節限定冰川行程並詢問興趣。'
                               '不能只說等下次開放或立即索取QR Code；不自動換線、不套常規線照片或報價。'
                               '客人要完整冬遊資料、團期或費用時靜默交接，內部註明冬遊需求。')
    return result


def validate_pricing(policy):
    if not isinstance(policy, dict) or not policy.get('years') or not policy.get('bands'):
        raise ValueError('seasonal_pricing_invalid')
    if any(type(y) is not int or not 2000 <= y <= 2100 for y in policy['years']):
        raise ValueError('seasonal_pricing_year_invalid')
    if type(policy.get('minimum_notice_days')) is not int or not 1 <= policy['minimum_notice_days'] <= 365:
        raise ValueError('seasonal_pricing_notice_invalid')
    _validate_private_departure(policy)
    _validate_departure_overrides(policy)
    if type(policy.get('weekday')) is not int or not 0 <= policy['weekday'] <= 6:
        raise ValueError('seasonal_pricing_weekday_invalid')
    if not isinstance(policy.get('advisor_months', []), list) or any(type(m) is not int or not 1 <= m <= 12 for m in policy.get('advisor_months', [])):
        raise ValueError('seasonal_pricing_month_invalid')
    if (not isinstance(policy.get('review_months', []), list)
            or any(type(m) is not int or not 1 <= m <= 12 for m in policy.get('review_months', []))
            or (policy.get('review_months') and not str(policy.get('review_reason') or '').strip())):
        raise ValueError('seasonal_pricing_review_invalid')
    groups = [policy.get('holiday_ranges', []), policy.get('unavailable_ranges', [])]
    recommendation = policy.get('winter_recommendation')
    if recommendation is not None and (not isinstance(recommendation, dict)
            or recommendation.get('months') != [12, 1, 2]
            or any(not isinstance(recommendation.get(k), str) or not recommendation[k].strip()
                   for k in ('name', 'explanation', 'source_ref'))):
        raise ValueError('seasonal_pricing_winter_recommendation_invalid')
    ids = set()
    for band in policy['bands']:
        if not isinstance(band.get('script_id'), str) or not band['script_id'].startswith('price_') or band['script_id'] in ids:
            raise ValueError('seasonal_pricing_script_invalid')
        ids.add(band['script_id'])
        if any(type(band.get(k)) is not int or band[k] <= 0 for k in ('price_cny', 'single_supplement_cny')):
            raise ValueError('seasonal_pricing_amount_invalid')
        groups.append(band['ranges'])
    for ranges in groups:
        if not isinstance(ranges, list): raise ValueError('seasonal_pricing_range_invalid')
        for pair in ranges:
            if not isinstance(pair, list) or len(pair) != 2: raise ValueError('seasonal_pricing_range_invalid')
            begin, end = [date.fromisoformat('2028-' + x) for x in pair]
            if begin > end: raise ValueError('seasonal_pricing_range_order')


def departure_quote(bundle, route, start, end, now, *, party_size=None, group_type='undecided'):
    if party_size is not None and (type(party_size) is not int or party_size < 1):
        raise ValueError('party_size_invalid')
    if group_type not in ('undecided', 'shared', 'private'):
        raise ValueError('group_type_invalid')
    spec = bundle.get('routes', {}).get(route, {})
    seasonal = spec.get('seasonal_pricing')
    policy = seasonal or spec.get('departure_availability')
    if not policy:
        return {'status':'not_seasonal', 'reason':'請使用該線路當前報價資料'}
    try:
        begin, finish = date.fromisoformat(start), date.fromisoformat(end or start)
        today = datetime.fromisoformat(now.replace('Z', '+00:00')).astimezone(ZoneInfo('Asia/Taipei')).date()
    except (ValueError, TypeError):
        return {'status':'ask_date', 'reason':'需要客人已表達的出發日期或日期範圍，不能猜日期'}
    if finish < begin or (finish-begin).days > 366:
        return {'status':'ask_date', 'reason':'日期範圍不完整或過長'}
    if begin < today:
        return {'status':'ask_date', 'reason':'出發日期已過，先確認年份'}
    days = [begin + timedelta(days=i) for i in range((finish-begin).days+1)]
    if (begin-today).days < policy['minimum_notice_days']:
        return {'status':'advisor', 'reason':'不足30天辦理入藏函，需先由顧問確認'}
    if any(y not in policy['years'] for y in range(begin.year, finish.year+1)):
        return _unavailable_result(bundle, route, days, today, '這個年份的本線團期尚未公布，不代表公司沒有其他產品。', 'year_unpublished', party_size, group_type)
    if not any(_within_operating_period(d, policy) for d in days):
        return _unavailable_result(bundle, route, days, today, '本線在這個日期沒有已公布團期。', 'season_closed', party_size, group_type)
    if not all(_within_operating_period(d, policy) for d in days):
        return {'status': 'ask_date', 'reason': '詢問範圍超出本線已公布出發期間，請確認適用的日期。'}
    specials = [item for item in policy.get('special_departures', []) if begin.isoformat() <= item['date'] <= finish.isoformat()]
    if specials:
        if party_size is not None and party_size >= 7 or group_type == 'private':
            return {'status': 'advisor', 'reason': '已公布特別團期為拼團價；獨團或7位以上需另行確認，不套特價或疊加常規加價。'}
        if begin == finish:
            special = specials[0]
            return {'status': 'quoted', 'route_variant': route, 'script_id': special['script_id'],
                    'departure_start': begin.isoformat(), 'departure_end': finish.isoformat(),
                    'departure_examples': [begin.isoformat()], 'price_cny': special['price_cny'],
                    'special_departure': True, 'group_type': 'shared',
                    'reason': '已確認指定日期的特別拼團價，優先於常規星期、冬季及長假規則；不代表剩餘名額，不外推其他日期或房差。'}
        return {'status': 'special_options', 'route_variant': route,
                'departures': [{'date': item['date'], 'price_cny': item['price_cny']} for item in specials],
                'reason': '此範圍含指定日期特價，先推薦適合的出發日；完整報價需核對該單日，不能把特價當整月或一般團期價。'}
    def overlaps(ranges):
        return any(_in_ranges(d, ranges) for d in days)
    if not seasonal:
        departures = _available_departures(spec, days, today)
        if not departures and not overlaps(policy['ranges']):
            return _unavailable_result(bundle, route, days, today, '本線是季節性行程，您詢問的時段目前沒有開放團期。', 'season_closed', party_size, group_type)
        if not departures:
            eligible_days = [d for d in days if _in_ranges(d, policy['ranges'])]
            return _non_scheduled_result(spec, eligible_days, today, party_size, group_type)
        return {'status': 'not_seasonal', 'availability_checked': True,
                'departure_examples': departures[:3], 'reason': '可介紹這些已公布團期，名額仍需確認；問價時使用本線現行模板。'}
    if all(_in_ranges(d, policy.get('unavailable_ranges', [])) for d in days):
        return _unavailable_result(bundle, route, days, today, '本線是季節性行程，您詢問的時段目前沒有開放團期。', 'season_closed', party_size, group_type)
    if overlaps(policy.get('holiday_ranges', [])):
        return {'status':'holiday', 'reason':'與長假日期重疊，不報普通價；先問是否有彈性，確定只能此時則靜默轉人工'}
    if any(d.month in policy.get('advisor_months', []) for d in days):
        return _unavailable_result(bundle, route, days, today, '冬季需另用冬遊產品，當前可自動介紹的目錄尚未開放冬遊；不能用常規線代替。', 'winter_catalog_unavailable', party_size, group_type)
    if any(d.month in policy.get('review_months', []) for d in days):
        return {'status':'advisor', 'reason_code':'business_confirmation_required',
                'reason':policy['review_reason']}
    if overlaps(policy.get('unavailable_ranges', [])):
        return {'status': 'ask_date', 'reason': '詢問範圍包含本線尚未開放的日期，先確認可否改到已公布團期，不猜出發日。'}
    matches = [b for b in policy['bands'] if all(any(lo <= d.strftime('%m-%d') <= hi for lo,hi in b['ranges']) for d in days)]
    if len(matches) != 1:
        return {'status':'ask_date', 'reason':'日期跨季或缺價，先縮小出發範圍，不能自行選最低價'}
    departures = [d for d in days if d.weekday() == policy['weekday']]
    if not departures:
        return _non_scheduled_result(spec, days, today, party_size, group_type)
    band = matches[0]
    return {'status':'quoted', 'route_variant':route, 'script_id':band['script_id'],
            'departure_start':begin.isoformat(), 'departure_end':finish.isoformat(),
            'departure_examples':[d.isoformat() for d in departures[:3]], 'price_cny':band['price_cny'],
            'single_supplement_cny':band['single_supplement_cny']}


def contains_trip_quote(text):
    # Block whole-trip quotes in common monetary formats while still allowing
    # answers about small per-person charges (for example a 60-yuan gratuity).
    number = r'\d[\d,]*(?:\.\d+)?'
    pattern = (rf'(?P<per_person>{number})\s*元?\s*/\s*人'
               rf'|每人\s*(?:¥|￥|人民幣|人民币|RMB|CNY)?\s*(?P<each>{number})\s*元'
               rf'|(?:團費|团费|優惠價|优惠价|精緻小團)[^。\n\d]{{0,60}}(?P<label>{number})'
               rf'|(?:人民幣|人民币|RMB|CNY|¥|￥)\s*[¥￥]?\s*(?P<currency>{number})'
               rf'|(?P<yuan>{number})\s*元')
    for match in re.finditer(pattern, text, re.I):
        group = next(k for k, v in match.groupdict().items() if v is not None)
        amount = match.group(group)
        if group != 'label' and float(amount.replace(',', '')) < 1000:
            continue
        # A separately asked room/flight/tip amount is not a whole-trip quote.
        # Use the nearest fee subject in this clause, so mentioning a room
        # supplement earlier cannot exempt a later unvalidated tour price.
        prefix = re.split(r'[。！？!?；;\n，,]', text[:match.start(group)])[-1]
        subjects = list(re.finditer(r'(?P<extra>單人?房差|单人?房差|房差|機票|机票|小費|小费|賠償|赔偿|預算|预算'
                                    r'|(?:獨立成團|独立成团)(?:加價|加价)|(?:基礎|基础)上(?:每人)?(?:另)?加(?:收)?'
                                    r'|(?:另|額外|额外)加(?:收)?|(?:每人|每位)(?:需)?(?:另)?加(?:收)?|加完|再加)'
                                    r'|(?P<trip>團費|团费|行程|優惠價|优惠价|總價|总价)', prefix))
        if subjects and subjects[-1].group('extra'):
            continue
        return True
    return False


def quote_error(decision, bundle, checked, availability=None, *, require_interrupt=True):
    route = decision['route_variant']
    spec = bundle['routes'].get(route, {})
    if ((availability or {}).get(route, {}).get('reason_code') == 'business_confirmation_required'
            and decision['action'] != 'handoff' and decision.get('start_introduction')):
        return '本線此時段銷售口徑待確認，不開始完整介紹；可推薦已確認且符合客人日期的產品，堅持本線則靜默交顧問核對。'
    if (availability or {}).get(route, {}).get('status') == 'private_departure' and decision['action'] != 'handoff':
        if any((m.get('script_id') or '').startswith('price') or contains_trip_quote(m.get('text', ''))
               for m in decision['messages']):
            return '自選日期已符合獨團人數門檻，但不是拼團報價授權；不套定班價。只問日期先說可自選；問實際獨團總價則靜默handoff。'
    if (availability or {}).get(route, {}).get('status') == 'unavailable' and decision['action'] != 'handoff':
        if (decision.get('start_introduction') or (require_interrupt and not decision.get('interrupt'))
                or decision['action'] == 'queue'
                or (availability[route].get('winter_recommendation') and
                    any(m.get('asset_keys') for m in decision['messages']))
                or any((m.get('script_id') or '').startswith('price') or contains_trip_quote(m.get('text', ''))
                       for m in decision['messages'])):
            return ('已核對本線在客人所問時段未開放：本輪不開始介紹、不queue，'
                    + ('interrupt=true，' if require_interrupt else '沉默回合沒有進行中的介紹，不要求interrupt=true，') +
                    '停止原介紹且不報價；冬遊推薦只用已確認說明，不發常規線素材；依工具返回的同期產品先推薦，否則依留資偏好輕邀開放通知。'
                    '僅邀請不能直接交接，實際留資或明確要求真人仍靜默handoff。')
    if not spec.get('seasonal_pricing') or decision['action'] == 'handoff':
        return None
    scripts = {s['id']:s['answer_text'] for s in spec['scripts']}
    for message in decision['messages']:
        script = message.get('script_id') or ''
        if script.startswith('price_'):
            if (route, script) not in checked:
                return '季節報價未核對：先呼叫 get_departure_quote，只有 status=quoted 才引用它返回的 script_id。'
            if message.get('text') and message['text'] != scripts.get(script):
                return '季節報價用已核對的 script_id 並把 text 留空；輕鬆收尾問句另放一條。'
        elif contains_trip_quote(message.get('text', '')):
            return '常規線團費不能自由改寫或跳過日期核對；先呼叫get_departure_quote，再以price_* script_id引用完整模板。'
    return None
