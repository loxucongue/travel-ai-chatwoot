"""Evidence-backed traveller document scope shared by replies and date checks."""
import copy
import re
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

KEY = '_traveler_documents'


class DocumentEvidence(BaseModel):
    model_config = ConfigDict(extra='forbid')
    message_id: str
    quote: str = Field(min_length=1, max_length=800)


class TravelerDocuments(BaseModel):
    model_config = ConfigDict(extra='forbid')
    residence: Literal['unknown', 'hong_kong', 'macao', 'taiwan', 'other'] = 'unknown'
    document: Literal['unknown', 'home_return', 'non_chinese_permit', 'taiwan_permit', 'foreign_passport', 'mixed'] = 'unknown'
    scope: Literal['unknown', 'self', 'all', 'mixed'] = 'unknown'
    evidence: list[DocumentEvidence] = Field(default_factory=list, max_length=6)


EXTRACTION_INSTRUCTION = '''traveler_documents只记录客户本人明确说出的居住身份及本次使用证件，附客户原话message_id和quote；不要凭粤语、出发机场、电话号码或我方旧回复推断身份。香港/澳门身份与回乡证分别确认，回乡证不等于非中国籍通行证；外国护照、台湾同行不能归为全团回乡证。只说“我是香港人”则document=unknown；明确使用回乡证才填home_return。scope=self仅本人、all全体、mixed混合同行。未新增信息输出默认unknown和空evidence；更正采用最新原话；不能从“有回乡证吗”这种问题推定持有。'''

REPLY_INSTRUCTION = '''证件规则优先于通用入藏函话术：先答客户当前问题。粤语线索只在身份未知时自然问是否香港/澳门旅客及是否使用回乡证，不直接认定。已确认的信息不重复问。只有确认适用普通回乡证的港澳旅客才说明无需入藏函，不再要求成都交函、办函资料或30天办函期限；本人证件不代表同行者。未知证件先确认，不宣布需要或免办；非中国籍通行证、外国护照、混合证件交顾问核对，不套港澳全团免办。免办不等于任意日期能接待或能提前入团，沿用实际团期与接待安排。边防证无需主动解释，客户问到手续时说明珠峰等部分地区另需办理、我们会协助在西藏办理，不保证即时办好或免手续。客户无相关问题时不展开证件清单。'''


def validate_extraction(value, history):
    value = TravelerDocuments.model_validate(value or {}).model_dump()
    messages = {str(m['id']): m for m in history}
    for evidence in value['evidence']:
        m = messages.get(evidence['message_id'])
        if not m or m.get('speaker') != 'customer' or evidence['quote'] not in m.get('text', ''):
            raise ValueError('traveler_document_requires_customer_evidence')
    if any(value[k] != 'unknown' for k in ('residence', 'document', 'scope')) and not value['evidence']:
        raise ValueError('traveler_document_missing_evidence')
    return value


def apply_understanding(context, understanding):
    """Replace on explicit new evidence; never let an unrelated turn erase it."""
    value = understanding.get('traveler_documents') or {}
    if not value.get('evidence'):
        return context
    return {**context, 'profile': {**context.get('profile', {}), KEY: value}}


def status(profile):
    value = (profile or {}).get(KEY) or {}
    if not value.get('evidence'):
        return 'unknown'
    if value.get('scope') == 'mixed' or value.get('document') in ('mixed', 'foreign_passport', 'non_chinese_permit'):
        return 'review'
    if value.get('document') == 'taiwan_permit':
        return 'required'
    if (value.get('residence') in ('hong_kong', 'macao') and value.get('document') == 'home_return'
            and value.get('scope') in ('self', 'all')):
        return 'exempt'
    return 'unknown'


def task(context):
    relevant = {context.get('route_variant'), context.get('profile', {}).get('discussion_route'),
                *(context.get('route_understanding') or {}).get('discussed_routes', [])}
    if not any(is_tibet(r or '', context.get('skills', {}).get('routes', {}).get(r, {})) for r in relevant):
        return {'applicable': False, 'ask_origin': False}
    incoming = [m.get('content', '') for m in context.get('messages', [])
                if m.get('direction') == 'incoming' and not m.get('private')][-3:]
    value = context.get('profile', {}).get(KEY) or {}
    asked_origin = any(m.get('direction') == 'outgoing'
        and m.get('status') in ('submitted', 'sent', 'delivered', 'read', 'simulated_delivered')
        and re.search(r'香港|澳門|澳门|哪裡.{0,4}旅客|哪裏.{0,4}旅客', m.get('content', ''))
        and re.search(r'？|\?', m.get('content', ''))
        for m in context.get('messages', []) if not m.get('private'))
    return {'applicable': True, 'permit_status': status(context.get('profile')), 'confirmed': value,
            'ask_origin': not asked_origin and value.get('residence', 'unknown') == 'unknown'
                and bool(re.search(r'係邊|系邊|喺邊|邊度|幾錢|唔該|我哋|你哋|點樣', ' '.join(incoming))),
            'instruction': REPLY_INSTRUCTION}


def is_tibet(route, spec):
    return route.startswith(('tibet_', 'peach_', 'ali_', 'ngari_')) or '西藏' in spec.get('name', '')


def scoped_bundle(bundle, profile):
    """Drop only the Tibetan permit lead time, never another product's lead time."""
    if status(profile) != 'exempt':
        return bundle
    result = copy.deepcopy(bundle)
    for route, spec in result.get('routes', {}).items():
        if not is_tibet(route, spec):
            continue
        for key in ('seasonal_pricing', 'departure_availability'):
            policy = spec.get(key)
            if policy and policy.get('minimum_notice_days') == 30:
                policy['minimum_notice_days'] = policy.get('operational_notice_days', 0)
    return result


def scoped_text(text, profile):
    """Suppress inapplicable permit sentences in templates, retaining other facts."""
    if not text or status(profile) == 'required':
        return text
    clauses = re.split(r'(?<=[。！？\n])', text)
    kept = []
    for clause in clauses:
        if not re.search(r'入[藏臧]函|旅藏確認函|旅藏确认函', clause):
            kept.append(clause)
        elif re.search(r'包含|包括|含[：:]|包含[：:]', clause):
            kept.append(re.sub(r'入藏函[、，,\s]*', '', clause) if status(profile) == 'exempt'
                        else clause.replace('入藏函', '入藏函（按旅客證件適用）'))
        elif status(profile) == 'exempt' and re.search(r'無需|无需|不用|不需要|免辦|免办', clause):
            kept.append(clause)
        elif re.search(r'？|\?|如果|是否|需不需要', clause):
            kept.append(clause)
    return ''.join(kept).strip()


def reply_error(decision, context):
    if not task(context).get('applicable'):
        return None
    state = status(context.get('profile'))
    public_text = '\n'.join(m.get('text') or '' for m in decision.get('messages', []))
    text = public_text + '\n' + (decision.get('handoff_reason') or '')
    latest = next((m.get('content', '') for m in reversed(context.get('messages', []))
                   if m.get('direction') == 'incoming' and not m.get('private')), '')
    if (re.search(r'邊防證|边防证', public_text)
            and not re.search(r'邊防|边防|邊境|边境|珠峰.{0,12}(?:證|证|手續|手续)|(?:證|证|手續|手续).{0,12}珠峰', latest)):
        return '客户本轮没有问边防证或珠峰相关手续，不主动展开边防证，先答当前问题。'
    if (context.get('event') == 'customer_message' and decision.get('action') == 'reply'
            and not decision.get('stop_followup') and not decision.get('opt_out')
            and task(context)['ask_origin']
            and not (re.search(r'香港|澳門|澳门', text) and re.search(r'？|\?', text))):
        return '本轮有粤语线索且身份未知、历史也未问过身份。先答问题，再自然询问是否香港或澳门旅客及本次是否用回乡证；不能凭粤语直接认定身份。'
    if state == 'exempt' and re.search(r'成都.{0,12}(?:交|領|领).{0,8}函|(?:不足|提前|至少)\s*30\s*天.{0,12}入藏函', text):
        return '已确认港澳回乡证旅客，不适用成都交函及30天入藏函期限。请先答本轮问题。'
    if state in ('unknown', 'required') and re.search(r'(?:您|你們|你们|大家).{0,10}(?:無需|无需|不用|不需要).{0,5}入藏函', text):
        return '尚未确认适用免办的证件，不能直接宣称客户或全体无需入藏函；先核实证件。'
    if state == 'review' and re.search(r'(?:你們|你们|大家|全團|全团).{0,8}(?:都|全部).{0,4}(?:無需|无需|不用|不需要).{0,5}入藏函', text):
        return '不同证件不能当作全团免办；按客户各自证件说明，顾问核对未确定部分。'
    return None
