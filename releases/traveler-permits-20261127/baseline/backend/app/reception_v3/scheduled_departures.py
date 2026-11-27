"""Exact published departures; no inferred weekly schedule or shared discounts."""
from datetime import date


def validate(policy):
    if (not isinstance(policy.get('years'), list) or not policy['years']
            or any(type(y) is not int or not 2000 <= y <= 2100 for y in policy['years'])
            or type(policy.get('minimum_notice_days')) is not int
            or not 1 <= policy['minimum_notice_days'] <= 365
            or not isinstance(policy.get('departures'), list) or not policy['departures']):
        raise ValueError('fixed_departures_invalid')
    seen, scripts = set(), set()
    for entry in policy['departures']:
        day = date.fromisoformat(entry['date'])
        script = entry.get('script_id', '')
        if (day.year not in policy['years'] or day in seen or script in scripts
                or not script.startswith('price_')
                or any(type(entry.get(k)) is not int or entry[k] <= 0
                       for k in ('price_cny','single_supplement_cny','minimum_party_size'))
                or not isinstance(entry.get('advisory', ''), str)):
            raise ValueError('fixed_departure_invalid')
        seen.add(day); scripts.add(script)


def quote(route, policy, begin, finish, today, party_size, group_type):
    entries = [e for e in policy['departures'] if begin <= date.fromisoformat(e['date']) <= finish]
    if not entries:
        return {'status':'unavailable','reason_code':'date_unpublished',
                'reason':'本線在詢問範圍沒有已公布團期；不要推算每週或其他年份團期。',
                'published_departures':[e['date'] for e in policy['departures'] if date.fromisoformat(e['date']) >= today]}
    if group_type == 'private' or party_size is not None and party_size >= 7:
        return {'status':'advisor','reason':'公布金額為拼團每人價；獨立包團須顧問核對，不套拼團價或常規線多人折扣。'}
    if len(entries) > 1:
        return {'status':'ask_date','reason':'詢問範圍有多個獨立團期，請確認想選哪個月份；不自行選便宜日期。',
                'departure_examples':[e['date'] for e in entries]}
    entry = entries[0]
    if (date.fromisoformat(entry['date']) - today).days < policy['minimum_notice_days']:
        return {'status':'advisor','reason':'距實際團期不足所需辦證時間，請顧問先核對。'}
    if entry['minimum_party_size'] > 1 and (party_size is None or party_size < entry['minimum_party_size']):
        return {'status':'ask_date','reason':'這個團期需先確認報名人數達到指定門檻；人數不足請討論其他公布團期，不擅自改日期。',
                'minimum_party_size':entry['minimum_party_size'], 'party_size':party_size,
                'departure_examples':[e['date'] for e in policy['departures'] if date.fromisoformat(e['date']) > date.fromisoformat(entry['date'])][:2]}
    return {'status':'quoted','route_variant':route,'script_id':entry['script_id'],
            'departure_start':entry['date'],'departure_end':entry['date'],'departure_examples':[entry['date']],
            'price_cny':entry['price_cny'],'single_supplement_cny':entry['single_supplement_cny'],
            'advisory':entry.get('advisory',''),'reason':'已公布拼團每人價，名額仍需確認；條件提示須先向客人說明，不外推其他日期。'}
