"""Keep an offered alternative separate from a customer's selected itinerary."""
import re

LINZHI = 'tibet_linzhi_8d'
EVEREST_ROUTES = frozenset(('peach_11d_2027', 'tibet_essence_8d', 'tibet_overview_10d'))
PEACH_ROUTES = frozenset(('peach_9d_2027', 'peach_11d_2027'))
ROUTE_DAYS = {'peach_9d_2027': 9, 'peach_11d_2027': 11, LINZHI: 8,
              'tibet_essence_8d': 8, 'tibet_overview_10d': 10, 'tibet_winter_8d': 8}


def reply_route(decision, previous=None, selected_route=''):
    """Resolve this reply's product without accepting a recommendation for the customer."""
    if decision.get('route_choice') == 'recommend':
        return {**(previous or {}), **decision.get('profile', {})}.get('discussion_route', '')
    return decision.get('route_variant') or selected_route


def advertisement_route(text, routes):
    """Match whole configured product titles, with the same punctuation as labels.

    Short intent aliases such as '不上珠峰' are not advertisement cards.
    """
    from app.engagement_labels import normalized, route_title_key
    text = re.sub(r'^\d{1,2}[~～–-]\d{1,2}月[|｜]', '', normalized(text))
    matched = []
    for key, spec in routes.items():
        titles = [key, spec['name'], *[a for a in spec.get('aliases', [])
                  if re.search(r'(?:\d+|[一二三四五六七八九十]+)\s*(?:天|日|[dD]|-day)', a)]]
        if route_title_key(text) in {route_title_key(a) for a in titles}:
            matched.append(key)
    return matched[0] if len(matched) == 1 else ''


def entry_comparison(messages, routes):
    """Only consecutive exact advertisement entries, never free-text preferences.

    The caller restricts this to intake before any itinerary has started.
    Product pairing and customer wording belong to the editable route package.
    """
    from datetime import datetime
    incoming = [m for m in messages if m.get('direction') == 'incoming']
    if len(incoming) < 2 or any(m.get('attachments') for m in incoming):
        return None
    try:
        elapsed = (datetime.fromisoformat(incoming[-1]['created_at'].replace('Z', '+00:00')) -
                   datetime.fromisoformat(incoming[0]['created_at'].replace('Z', '+00:00'))).total_seconds()
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    if not 0 <= elapsed <= 120:
        return None
    selected = set()
    for message in incoming:
        route = advertisement_route(message.get('content') or '', routes)
        if not route:
            return None
        selected.add(route)
    if len(selected) != 2:
        return None
    first, second = sorted(selected)
    rule = routes[first].get('entry_comparison') or {}
    reverse = routes[second].get('entry_comparison') or {}
    if (rule.get('other_route') != second or reverse.get('other_route') != first
            or rule.get('preferred_route') != reverse.get('preferred_route')
            or rule.get('summary') != reverse.get('summary')
            or rule.get('preferred_route') not in selected or not rule.get('summary')):
        return None
    return {'routes': sorted(selected), 'preferred_route': rule['preferred_route'], 'summary': rule['summary']}


def rejects_current_route(route, profile):
    return ((profile.get('everest_preference') == 'avoid' and route in EVEREST_ROUTES)
            or (profile.get('peach_preference') == 'avoid' and route in PEACH_ROUTES))


def rejected_asset(key, materials, profile):
    routes = next((m.get('routes', []) for m in materials if m['key'] == key), [])
    return bool(routes) and all(rejects_current_route(r, profile) for r in routes)


def choice_error(decision, context):
    routes = context['skills']['routes']
    understanding = context.get('route_understanding')
    old = context.get('route_variant', '')
    selected = decision['route_variant'] or old
    changing = bool(selected and selected != old)
    if (understanding and understanding['intent'] == 'select' and decision['action'] != 'handoff'
            and selected != understanding['selected_route'] and decision['start_introduction']):
        return '本輪已選擇route_understanding.selected_route，不能開始其他線路介紹；若日期等客觀條件不符，先答明限制，不播放舊線介紹。'
    if understanding and decision['action'] != 'handoff' and changing:
        if (understanding['intent'] != 'select' or understanding['selected_route'] != selected):
            return '本輪語義是詢問、比較或尚未選定，不能變更已選線路；依route_understanding承接問題，討論其他線可用recommend，不開始整套介紹。'
    # A delivered reference map is evidence of a preview, not a product choice.
    # Validate this transition independently of product IDs or script wording.
    if (not context.get('route_variant') and decision['route_variant']
            and decision['action'] != 'handoff' and not context.get('state', {}).get('human_followup')):
        from app.reception_v3.material_rules import itinerary_previews
        maps = itinerary_previews(context['skills'], context.get('available_materials', []),
                                  context.get('messages', []))
        if any(a['already_delivered'] for r in maps.values() for a in r['assets']):
            if (context.get('event') != 'customer_message'
                    or decision.get('route_choice') != 'selected'):
                return '上方只是參考行程圖；詢問詳情或價錢不等於正式選線。保留空route_variant，不開始整套介紹；需討論此線時用recommend及discussion_route。'
    if (not (PEACH_ROUTES & routes.keys() or LINZHI in routes) or decision['action'] == 'handoff'
            or context.get('state', {}).get('human_followup')):
        return None
    old = context.get('route_variant', '')
    selected = decision['route_variant'] or old
    profile = {**context.get('profile', {}), **decision.get('profile', {})}
    choice = decision.get('route_choice', 'unchanged')
    if choice == 'selected' and not decision['route_variant']:
        return '確認選線時route_variant必須填明確的已啟用線路ID。'
    if selected and selected not in routes:
        return '只能選擇本輪已啟用的線路。'
    if ('everest_preference' in decision.get('profile', {})
            and decision['profile']['everest_preference'] not in ('avoid', 'want', 'undecided')):
        return 'everest_preference只用avoid、want或undecided；僅擔心高反不等於拒絕珠峰。'
    if 'altitude_concern' in decision.get('profile', {}) and type(decision['profile']['altitude_concern']) is not bool:
        return 'altitude_concern必須是布林值。'
    if ('peach_preference' in decision.get('profile', {})
            and decision['profile']['peach_preference'] not in ('avoid', 'want', 'undecided')):
        return 'peach_preference只用avoid、want或undecided；想去珠峰不等於拒絕桃花。'
    if context.get('event') != 'customer_message' and (
            choice != 'unchanged' or (old and selected != old)
            or any(k in decision.get('profile', {}) and decision['profile'][k] != context.get('profile',{}).get(k)
                   for k in ('everest_preference', 'peach_preference', 'altitude_concern', 'discussion_route'))):
        return '沉默或介紹完成事件不能替客人選線、改偏好或建立新的轉薦；保留已確認選擇。'
    if choice == 'recommend':
        suggested = profile.get('discussion_route')
        if suggested not in routes or suggested == old:
            return '推薦時profile.discussion_route填已啟用的替代線路，route_variant保留原選擇。'
        if selected != old or decision['start_introduction']:
            return '推薦不等於同意換線：保留原route_variant，start_introduction=false；可先發替代線路圖和簡短差異。'
        if rejects_current_route(suggested, profile):
            return '不能再次推薦客人已明確拒絕的桃花或珠峰產品；除非客人本輪主動改變偏好。'
        if context.get('state', {}).get('delivery_kind') in ('intake', 'introduction') and not decision['interrupt']:
            return '正在推薦替代線，interrupt=true暫停原線未發送圖文，不能一邊比較一邊繼續舊介紹。'
    target = profile.get('discussion_route') if choice == 'recommend' else selected
    days = profile.get('trip_days')
    if (target in PEACH_ROUTES and isinstance(days, (int, float)) and not isinstance(days, bool)
            and days < ROUTE_DAYS[target] and (choice in ('recommend', 'selected') or decision['start_introduction'])):
        return '客人可用天數不足，不能直接推薦或開始較長的桃花行程；先確認是否可延長，不能把8天當9天或11天。'
    if old and selected != old:
        if choice != 'selected':
            return '客人明確接受或要求改線才route_choice=selected；月份回答、單問高反或詢問替代方案用recommend，不直接換線。'
    if old and selected != old and context.get('state', {}).get('delivery_kind') in ('intake', 'introduction') and not decision['interrupt']:
        return '切線時interrupt=true，先取消原線尚未發送的圖文。'
    if rejects_current_route(selected, profile):
        if decision['start_introduction'] or decision['action'] == 'queue':
            return '客人已拒絕當前產品的桃花或珠峰安排，停止原介紹；先推薦匹配產品，不queue、不開始舊線介紹。'
        if context.get('state', {}).get('delivery_kind') in ('intake', 'introduction') and not decision['interrupt']:
            return '已拒絕當前線但尚未選定替代線，仍須interrupt=true停止原圖文。'
        if (choice != 'recommend' and
                any((m.get('script_id') or '').startswith('price') for m in decision['messages'])):
            return '已拒絕當前線，不能引用原線價格；推薦後等客人確認替代產品。'
        if context.get('event') == 'silence_due' and decision['messages']:
            return '客人已拒絕當前線且尚未選定替代線，本輪wait，不繼續舊線推廣或替客人改線。'
    for message in decision['messages']:
        if any(rejected_asset(k, context.get('available_materials', []), profile) for k in message['asset_keys']):
            return '不發僅適用於客人已拒絕產品的照片或行程圖；使用符合最新偏好的資料。'
    return None
