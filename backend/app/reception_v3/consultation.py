"""Evidence and delivered-question memory shared by every route."""
from typing import Literal
import re
from pydantic import BaseModel, ConfigDict, Field

DELIVERED = {'simulated_delivered', 'submitted', 'sent', 'delivered', 'read'}
TOPICS = ('departure', 'party', 'companions', 'concern')
PARTY_FIELDS = ('party_size', 'party_size_min', 'party_size_range')


class NeedEvidence(BaseModel):
    model_config = ConfigDict(extra='forbid')
    topic: Literal['departure', 'party', 'companions', 'concern']
    status: Literal['known', 'undecided', 'declined']
    message_id: str
    quote: str = Field(min_length=1)


class AskedEvidence(BaseModel):
    model_config = ConfigDict(extra='forbid')
    topic: Literal['departure', 'party', 'companions', 'concern']
    message_id: str


class PartyEvidence(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['actual', 'product_capacity', 'hypothetical']
    message_id: str
    quote: str = Field(min_length=1)


def capacity_only(text):
    """Reject an unambiguous product-capacity fragment, not all numbered text."""
    compact = re.sub(r'\s+', '', text)
    capacity = re.search(r'(?:\d+|[二兩两四六])(?:[–—~～\-至到](?:\d+|[二兩两四六]))?人(?:小團|小团|精品團|精品团|即可成團|即可成团|成團|成团)', compact)
    actual = re.search(r'(?:我們|我们|我家|一家|一行|同行|報名|报名|預計|预计|共|共有)\D{0,5}(?:\d+|[一二兩两三四五六七八九十])\s*(?:位|人)|\d+大\d+小', compact)
    return bool(capacity and not actual)


def source(messages, key, direction):
    return next((m for m in messages if m.get('id') == key and not m.get('private')
                 and m.get('direction') == direction
                 and (direction == 'incoming' or m.get('status') in DELIVERED)), None)


def explicit_size(quote):
    """Check simple, explicit counts only; semantic interpretation stays in the model."""
    compact = re.sub(r'\s+', '', quote)
    children = re.search(r'(\d+)大(\d+)小', compact)
    if children:
        return sum(map(int, children.groups()))
    match = re.search(r'(?:我們|我们|我家|一家|一行|同行|報名|报名|預計|预计|共有|共)\D{0,5}?(\d+|[一二兩两三四五六七八九十])(?:位|人)(?!以上|以下|左右|起)', compact)
    if not match:
        match = re.fullmatch(r'(\d+|[一二兩两三四五六七八九十])(?:位|人)[。！!~～]*', compact)
    if match:
        number = match[1]
        return int(number) if number.isdigit() else dict(zip('一二兩两三四五六七八九十', (1,2,2,2,3,4,5,6,7,8,9,10)))[number]
    return None


def validation_error(decision, context):
    messages = context.get('messages', [])
    evidence = decision.get('party_evidence')
    changed = any(k in decision.get('profile', {}) and decision['profile'][k] is not None
                  and decision['profile'][k] != context.get('profile', {}).get(k) for k in PARTY_FIELDS)
    if changed:
        m = source(messages, (evidence or {}).get('message_id'), 'incoming')
        if (not evidence or evidence['kind'] != 'actual' or not m
                or evidence['quote'] not in m.get('content', '') or capacity_only(evidence['quote'])
                or re.match(r'\s*(如果|假如|假設|假设|若是|要是)', evidence['quote'])):
            return '写入实际同行人数须提供party_evidence：kind=actual、真实客户message_id及逐字原话；广告小团容量和假设询价人数不能写入profile。'
        count = explicit_size(evidence['quote'])
        if count is not None and decision['profile'].get('party_size') not in (None, count):
            return 'party_size与引用的客户实际人数不一致；请使用最新明确原话中的实际同行人数。'
    for item in decision.get('need_evidence', []):
        m = source(messages, item['message_id'], 'incoming')
        if not m or item['quote'] not in m.get('content', ''):
            return '需求状态必须引用真实客户消息和逐字原话；不能引用我方介绍。'
    for item in decision.get('asked_evidence', []):
        if not source(messages, item['message_id'], 'outgoing'):
            return '已问记录只能引用已实际发送的AI或人工消息。'
    questions = [m.get('question_topic') for m in decision.get('messages', []) if m.get('question_topic')]
    if len(questions) > 1:
        return '本轮最多选择一个需求问题，不连问月份、人数、同行构成及顾虑。'
    incoming = [m for m in messages if m.get('direction') == 'incoming']
    if incoming and all(capacity_only(m.get('content','')) for m in incoming) and not evidence:
        merged={**context.get('profile',{}),**decision.get('profile',{})}
        if any(merged.get(k) for k in PARTY_FIELDS) or merged.get('group_type')=='private':
            return '历史人数/独团判断只有广告容量依据，不能继续沿用。将party_size、party_size_min、party_size_range置null，group_type置undecided，再核实实际人数；不批量处理其他会话。'
        for message in decision.get('messages', []):
            if re.search(r'(?:你們|你们|您們|您们|所以)\s*\d+\s*(?:位|人)', message.get('text','')):
                return '目前只有广告容量，没有客户实际人数。不能对客户说你们几位或推断能否单独成团；按线路介绍并了解真实人数。'
    return None


def memory_updates(decision, messages, memory):
    needs = dict(memory.get('_consultation_needs') or {})
    for item in decision.get('need_evidence', []):
        needs[item['topic']] = dict(item)
    asked = dict(memory.get('_consultation_asked') or {})
    for item in decision.get('asked_evidence', []):
        if source(messages, item['message_id'], 'outgoing'):
            asked[item['topic']] = item['message_id']
    updates = {'_consultation_needs': needs, '_consultation_asked': asked}
    if (decision.get('party_evidence') or {}).get('kind') == 'actual':
        updates['_party_evidence'] = dict(decision['party_evidence'])
    return updates


def task(context):
    profile = context.get('profile', {})
    needs = dict(profile.get('_consultation_needs') or {})
    asked = dict(profile.get('_consultation_asked') or {})
    for m in context.get('messages', []):
        if m.get('direction') == 'outgoing' and m.get('status') in DELIVERED and m.get('question_topic'):
            asked[m['question_topic']] = m['id']
    known = set(k for k,v in needs.items() if v.get('status') in ('known','undecided','declined'))
    if profile.get('departure_window') or profile.get('departure_date_start'):
        known.add('departure')
    if any(profile.get(k) for k in PARTY_FIELDS):
        known.add('party')
    return {'needs': needs, 'asked': asked, 'missing_topics': [t for t in TOPICS if t not in known],
            'instruction': INSTRUCTION}


INSTRUCTION = '''全线路介绍后推进：先回答本轮问题，再选一个尚未了解且适合的问题，不是一轮问完清单。
客户看过介绍后首次说再看看/考虑，优先补未知可出行时间，再人数及长辈小孩，再具体顾虑；均已了解或客户主动想联系时自然邀请。
已说未定记need_evidence.status=undecided；不愿回答记declined，不反复逼问。刚问过未答不立即同义重问；客户明确让自己看看就给空间。
当前提问标messages.question_topic；历史人工/AI已问可用asked_evidence引用真实已发送message_id，不能将草稿当已问。
客户明确回答需求写need_evidence，引用客户原话；每次更新实际人数须party_evidence(kind=actual)，广告6人小团/4–6人容量与假设如果6人询价不是报名人数。
历史人数无来源且仅靠广告容量时不要沿用，清除错误party_size/party_size_min/party_size_range后自然核实，不能进入独团分支。
邀请提供完整资料及沟通安排时可任选LINE/微信/WhatsApp的QR Code或ID；尊重已有偏好、拒绝、已邀请和人工接管，不固定声称平台文件太卡。
阿里保留适用的体能年龄了解，但客户当前问题优先；介绍完成不等于客户读完，不把介绍前想看看当作本轮考虑。
不强制收齐资料才报价或联系；客户主动问价格先给有依据的费用，时间未知可给适用起价再问时间。'''
