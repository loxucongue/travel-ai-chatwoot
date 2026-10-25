"""Short advisor-facing summaries; diagnostic codes remain in internal records."""
import re


REASONS = {
    'contact': ('收到联系方式', '客户已提供联系方式，请接着联系。'),
    'contact_image': ('收到联系图片', '客户发来联系图片，请确认并联系。'),
    'contact_reported': ('客户已加好友', '客户表示已加好友，请确认并接待。'),
    'requested': ('客户找真人', '客户希望由顾问接待。'),
    'party_size': ('多人出行', '人数需要单独安排，请确认包团方案。'),
    'trip_days': ('行程天数调整', '客户需要调整天数，请确认合适行程。'),
    'custom': ('定制需求', '客户有特殊安排，请确认方案。'),
    'knowledge': ('问题待核实', '现有资料无法确认，请核实后回复客户。'),
    'label': ('人工接待', '会话已标记为需要顾问处理。'),
    'manual': ('人工接待', '请由顾问继续接待客户。'),
    'followup_customer_replied': ('客户回复跟进', '客户已回复，请顾问继续接待。'),
    'reply_queue_overdue': ('客户等待回复', '客户等待较久，请及时回复。'),
    'channel_window_closed': ('回复窗口结束', '渠道暂时无法主动发送，请顾问核实联系安排。'),
    'new_customer_activation_failed': ('自动接待未启动', '请顾问先接待客户。'),
    'delivery_failed': ('消息发送异常', '请核对消息是否送达，再继续接待。'),
    'delivery_unknown': ('消息送达待确认', '消息是否送达尚未确认，请先核对，避免重复发送。'),
    'model_error': ('自动回复异常', '自动回复未完成，请顾问接待。'),
    'system': ('自动回复异常', '自动回复未完成，请顾问接待。'),
    'other': ('需要人工协助', '请查看客户最新问题并继续接待。'),
}


def clean_reason(detail, fallback):
    text = str(detail or '').strip()
    if text.startswith('场景：') and '\n原因：' in text:
        text = text.split('\n原因：', 1)[1]
    # Historical V3 summaries placed a Python profile dictionary on the first line.
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    lines = [line for line in lines if not line.startswith(('{', '[', 'Traceback', 'File ', 'Chatwoot 标签触发'))]
    text = ' '.join(lines)
    if (not re.search(r'[\u3400-\u9fff]', text) or
            re.search(r'[{}\[\]`]|\b\w+_\w+\b|mention://|ai-notification:|handoff:\d|\bSLA\b', text)):
        return fallback
    text = re.sub(r'\s+', ' ', text).strip()
    # One short reason, not the model's full explanation or internal state.
    first = re.split(r'(?<=[。！？])', text)[0]
    return first if len(first) <= 80 else first[:77].rstrip('，、；： ') + '…'


def handoff_summary(code, detail=''):
    code = (code or 'other').removeprefix('handoff.')
    scene, fallback = REASONS.get(code, REASONS['other'])
    if code not in REASONS and any(word in code for word in ('error', 'failed', 'timeout')):
        scene, fallback = REASONS['system']
    reason = clean_reason(detail, fallback)
    return {'scene':scene, 'reason':reason, 'text':f'场景：{scene}\n原因：{reason}'}


def notification_summary(event, title, body):
    if event == 'handoff.overdue':
        return '场景：接待提醒\n原因：客户仍在等待，请及时回复。'
    if str(body).startswith('场景：') and '\n原因：' in body:
        scene, reason = body.split('\n原因：',1)
        return scene + '\n原因：' + clean_reason(reason, REASONS['other'][1])
    if event.startswith(('handoff.', 'assignment.')):
        scene = clean_reason(title, '需要人工协助')
        reason = clean_reason(body, REASONS['other'][1])
        return f'场景：{scene}\n原因：{reason}'
    return body
