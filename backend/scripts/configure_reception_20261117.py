"""Pure package upgrade; deployment supplies the current production package."""
from copy import deepcopy

GUIDANCE = '''2026-11-17全线路接待口径（优先于下方旧示例中冲突的接待顺序）：
广告中的小团容量不是实际同行人数。介绍后客户说再看看，先承接并了解一个未知且未反复问过的需求；已知、明确未定或不愿回答的不重问，最后适时邀请联系，不机械问卷。
客户询问大概费用而日期未知，可用本轮starting_price_evidence中的有效起价，说明每人币种、年份、团型与季节条件，再问出行时间；无有效起价则核对，不借其他线路价格。没有客户日期依据不自行查询某个月或制造临近办证紧迫感。
产品配置允许时，真实4人可在已开放范围申请新开拼团，不自动写成包团；5–6人或明确包团意向按现行产品配置主动说明适用安排，并自然邀请顾问承接。不承诺任何日期、资源最多或未经确认的免费升级，阿里固定团期仍以配置为准。
联系邀请可任选LINE／WhatsApp／微信的QR Code或ID，尊重指定渠道、已留资与拒绝，不声称平台文件卡顿。客户明确问桃花且已成功发送了解邀请后，才建立一次60分钟参考介绍；未明确偏好默认11日，明确不去珠峰适用9日，不改客户已选主线。
阿里首次介绍保持现行图文；后续按真实发送记录跳过重复主题、图片与内容，没有合适新内容则等待。'''


def upgrade(package):
    p = deepcopy(package)
    if not p.get('ai_guidance','').startswith(GUIDANCE):
        p['ai_guidance'] = GUIDANCE + '\n\n' + p.get('ai_guidance','')
    large = p.get('large_party') or {}
    text = large.get('contact_text','')
    if 'QR Code' in text and 'QR Code或ID' not in text:
        large['contact_text'] = text.replace('QR Code','QR Code或ID')
    elif 'QRCode' in text and 'QRCode或ID' not in text:
        large['contact_text'] = text.replace('QRCode','QRCode或ID')
    for policy in (p.get('followup_policy'), large.get('followup_policy')):
        if policy and 'peach_interest_wait' in policy.get('checkpoints',{}):
            policy['checkpoints']['peach_interest_wait'] = 60
            policy['guidance'] = policy.get('guidance','').replace('五分鐘','一小時').replace('5分鐘','60分鐘')
    return p
