"""Bound unselected-route follow-ups using actual customer and delivery history."""
from datetime import datetime, timedelta, timezone
import re

TOPICS = ('preference', 'suitable_plan', 'catalog', 'customization')
DELIVERED = {'simulated_delivered', 'submitted', 'sent', 'delivered', 'read'}
RESERVED = DELIVERED | {'submitting', 'submission_unknown'}
NEUTRAL = {'getstarted', '開始', '开始', '開始使用', '开始使用', 'hi', 'hello',
           '你好', '您好', '哈囉', '哈啰', '謝謝', '谢谢', '收到', '好的', '好',
           '嗯', '嗯嗯', 'ok', 'okay', '了解', '讚', '赞'}


def dt(value):
    stamp = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def substantive(message):
    # Persisted semantic decisions take precedence. Legacy histories fall back
    # conservatively to explicit customer speech, never an assistant's claims.
    if 'substantive_customer' in message:
        return message['substantive_customer'] is True
    text = re.sub(r'[\W_]+', '', message.get('content', '').casefold())
    return bool(text and text not in NEUTRAL)


def policy(silence, route='', controls=None, profile=None):
    value = silence.get('undecided_followup') or {}
    controls, profile = controls or {}, profile or {}
    state = controls.get('v3') or {}
    if (not value.get('enabled') or route or controls.get('human')
            or controls.get('human_followup', {}).get('enabled')
            or state.get('handoff') or state.get('lead_assist')
            or state.get('opt_out') or state.get('appointment_pending')
            or profile.get('reception_status') == 'abusive'):
        return None
    return value


def inspect(messages, profile, config, *, exclude_plan=None):
    incoming = [m for m in messages if m.get('direction') == 'incoming']
    meaningful = [m for m in incoming if substantive(m)]
    known = any(profile.get(k) for k in ('party_size', 'party_size_range', 'trip_days',
                'departure_window', 'departure_date_start', 'destination', 'inquiry_status'))
    level = 'needs_known' if meaningful or known else 'entry_only'
    anchor_message = meaningful[-1] if meaningful else incoming[0] if incoming else None
    anchor = (anchor_message or {}).get('created_at')
    anchor_index = next((i for i, m in enumerate(messages) if m is anchor_message), -1)
    used, touches = set(), set()
    for index, m in enumerate(messages):
        if not m.get('proactive') or m.get('status') not in RESERVED:
            continue
        if index <= anchor_index:
            continue
        if anchor and m.get('created_at') and dt(m['created_at']) < dt(anchor):
            continue
        identity = m.get('plan_id') or m.get('run_id') or m.get('id')
        if exclude_plan is not None and identity == exclude_plan:
            continue
        touches.add(identity)
        if m.get('followup_topic'):
            used.add(m['followup_topic'])
        if 'china2go.com/travel' in m.get('content', ''):
            used.add('catalog')
    # The directory is offered at most once even across a customer's new reply.
    if any(m.get('direction') == 'outgoing' and m.get('status') in RESERVED
           and 'china2go.com/travel' in m.get('content', '') for m in messages):
        used.add('catalog')
    maximum = config.get('needs_known_max', 3) if level == 'needs_known' else config.get('entry_only_max', 1)
    return {'level': level, 'consecutive_touches': len(touches), 'maximum': maximum,
            'remaining': max(0, maximum-len(touches)), 'used_topics': sorted(used),
            'available_topics': [t for t in TOPICS if t not in used], 'anchor': anchor}


def task(context):
    config = policy(context['skills'].get('silence', {}), context.get('route_variant', ''),
                    {'v3': context.get('state', {})}, context.get('profile', {}))
    if not config:
        return None
    result = inspect(context.get('messages', []), context.get('profile', {}), config)
    instruction = ('本轮是客户新消息，不是到期追问，不执行跟进主题。'
            'Get Started、你好、点赞只表示开启咨询，没有选择任何线路，也没有默认桃花线路。'
            '入口客户只做简短通用接待、询问一个旅行偏好；不自动选线、不发送某一条的整套介绍。')
    if context.get('event') == 'silence_due':
        instruction = ('这是未选线客户的轻跟进，不是刚收到客户消息。结合已知条件选一个未发主题，'
            '用一至三个繁体中文短句、最多一个轻问题。入口点击客户可给一次官网目录帮助选方向；'
            '已说2人5天就保留条件，不重问人数天数，也不把推荐北京当已选北京。'
            '主题填followup_topic。preference降低选择难度；suitable_plan补充符合条件且已确认的方案；'
            'catalog只给一次目录；customization只说可协助核对需求，不承诺全国办公室或任何行程都能订。'
            '若先前只问去哪且未回应，提供目录或选择维度是新的帮助，不必机械wait；'
            '没有适合的新内容仍wait。禁止索资、主动报价、擅自选线、重复问句；'
            '已明确价格顾虑先补充当前产品价值，不转为泛泛选目的地。')
    return {**result, 'catalog_url': 'https://china2go.com/travel/', 'instruction':instruction}


def validation_error(decision, context):
    # This is an output-format check only: it does not infer budgets, choose a
    # route, change facts or rewrite the model's answer.
    questions = context.get('buffered_questions') or [
        m for m in context.get('messages', []) if m.get('direction') == 'incoming'][-1:]
    customer_text = '\n'.join(m.get('content', '') for m in questions)
    if context.get('route_variant') == 'tibet_ali_15d':
        answer = '\n'.join(m.get('text', '') for m in decision.get('messages', []))
        if (decision.get('messages') and re.search(r'供氧|氧氣|氧气',customer_text)
                and re.search(r'酒店|住宿|飯店|饭店',customer_text)
                and re.search(r'贵|貴|[費费]用.{0,3}高',customer_text)
                and not re.search(r'止[熱热]寺[^。！？\n]{0,55}(?:無|无|沒有|没有|不提供)供氧',answer)):
            return '客户明确问酒店是否都供氧，必须先直接回答：「不是全程供氧，大部分住宿有供氧，止熱寺多人間無供氧。」不能删掉此例外而只回答价格。保留这个事实后再简短解释价值，请重新输出完整JSON。'
        for message in decision.get('messages', []):
            text = message.get('text', '')
            for match in re.finditer('全程供氧', text):
                if not re.search(r'不|非|無法|无法', text[max(0,match.start()-10):match.start()]):
                    return '阿里15天止热寺住宿无供氧，不能先声称全程供氧再补例外。请直接说大部分住宿供氧、止热寺无供氧，重新输出JSON。'
    if context.get('event') == 'customer_message' and re.search(r'贵|貴|[費费]用.{0,3}高', customer_text):
        if any(re.search(r'✅|🔹|(?m:^\s*(?:[-•]|\d+[.、])\s)', m.get('text', '')) for m in decision.get('messages', [])):
            return '价格异议回复的格式错误：必须使用自然短段落，不使用✅、🔹或条列清单。保留有依据的两三个价值点，不追加换方案、报价或留资邀请；请重新输出完整JSON。'
    current = task(context)
    if current and context.get('event') == 'customer_message' and current['level'] == 'entry_only':
        if decision.get('route_variant') or decision.get('start_introduction') or decision.get('route_choice') == 'selected':
            return '客户仅开启咨询或问候，没有选择任何线路。route_variant必须为空、route_choice保持unchanged、start_introduction=false，做通用接待。'
    if context.get('event') != 'silence_due' or not current or decision.get('action') == 'wait':
        return None
    if decision.get('action') == 'handoff' or not decision.get('messages'):
        return None
    if current['remaining'] <= 0:
        return '未选线跟进次数已用完，本轮必须wait。'
    if decision.get('followup_topic') not in current['available_topics']:
        return '本轮followup_topic必须选择尚未发送的可用主题；没有新内容用wait。'
    if decision.get('route_choice') == 'selected' or decision.get('start_introduction'):
        return '沉默不是客户选线确认；保留未选线状态，不启动整套介绍。'
    return None


def enforce(row, value, silence, *, exclude_plan=None):
    if not (silence.get('undecided_followup') or {}).get('enabled'):
        return False
    config = policy(silence, row.controls.get('route_variant', ''),
                    {**row.controls, 'v3': value}, row.memory)
    if not config:
        return False
    current = inspect(row.messages, row.memory, config, exclude_plan=exclude_plan)
    value['undecided_followup'] = current
    if current['remaining']:
        return False
    value.update(next_check_at=None, next_check_minutes=None,
                 followup_skip_reason='undecided_followup_limit')
    return True


def meta_window_open(channel, customer_at, now):
    """No verified ad-window evidence is available in our session contract.

    Chatwoot can_reply may include a human-agent window, so it is insufficient
    authorization for an automated marketing follow-up after 24 hours.
    """
    if channel not in ('Channel::FacebookPage', 'Channel::Instagram'):
        return True
    return bool(customer_at and timedelta(0) <= dt(now)-dt(customer_at) < timedelta(hours=24))
