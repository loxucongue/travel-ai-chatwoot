"""Reference floors from the same dated quote engine used for final quotes."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from app.reception_v3.seasonal_quotes import departure_quote


def reference(bundle, route, now, profile):
    spec = bundle['routes'][route]
    policy = spec.get('seasonal_pricing')
    if not policy:
        return None  # Fixed-price routes already expose their effective approved scripts.
    today = datetime.fromisoformat(now.replace('Z','+00:00')).astimezone(ZoneInfo('Asia/Taipei')).date()
    count = profile.get('party_size') or profile.get('party_size_min')
    group = profile.get('group_type', 'undecided')
    if group == 'private' and not policy.get('group_pricing'):
        return {'status':'advisor','reason':'独团无已配置价格，不能以拼团起价代替。'}
    found = []
    for year in policy.get('years', []):
        if year < today.year:
            continue
        day = max(date(year,1,1), today + timedelta(days=policy.get('minimum_notice_days',0)))
        while day.year == year:
            result = departure_quote(bundle, route, day.isoformat(), day.isoformat(), now, party_size=count, group_type=group)
            if result.get('status') == 'quoted':
                options = result.get('options') or [result]
                for item in options:
                    if type(item.get('price_cny')) is int:
                        found.append({'price_cny':item['price_cny'],'group_type':item.get('group_type', 'shared'),
                                      'example_departure':day.isoformat(),'script_id':item.get('quote_script_id') or result.get('script_id','')})
            day += timedelta(days=1)
    if not found:
        return {'status':'unavailable','reason':'现行价格数据没有可用于起价的未来适用团期；不要编造价格。'}
    minimum = min(x['price_cny'] for x in found)
    return {'status':'reference','currency':'CNY','unit':'per_person','price_cny':minimum,
            'examples':[x for x in found if x['price_cny']==minimum][:3],
            'instruction':'这是适用日期中的参考起价，不是客户最终报价，不表示全部日期有团；说明年份、团型及按季节安排核价。仅询问大概费用且日期未知时使用；已知日期按实际日期核价。'}
