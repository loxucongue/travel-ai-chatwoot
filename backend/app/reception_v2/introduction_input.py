"""Classify an interruption without generating or rewriting a customer reply."""
import json


def classify_introduction_input(text: str, route_variant: str) -> tuple[str, dict]:
    from app.reception_v2.runtime import _call, _request, _parse_json

    payload = _request([
        {'role': 'system', 'content':
         '客户正在接收已确认线路的整套介绍。只判断这条插话是否必须立即中止介绍。'
         '普通问题、补充人数日期、确认、催继续介绍都为queue，问题将在整套发完后集中回答。'
         '明确停止发送或拒绝联系为stop；明确改选另一线路为switch；'
         '要求真人、已给出实际联系方式为handoff。单纯比较另一线路仍为queue。'
         '引用或否定停止/真人不能当成要求。只输出JSON {"action":"queue|stop|switch|handoff"}。'},
        {'role': 'user', 'content': json.dumps({'route_variant': route_variant, 'message': text}, ensure_ascii=False)},
    ], tools=False)
    payload['max_tokens'] = 120
    message, log = _call(payload, 0)
    action = _parse_json(message.get('content')).get('action')
    if action not in {'queue', 'stop', 'switch', 'handoff'}:
        raise ValueError('invalid_introduction_input_action')
    return action, log
