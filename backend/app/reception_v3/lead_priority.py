"""Evidence-backed next-day contact priority within the existing follow-up task.

The reception model interprets questions/refusals; the executor owns dates,
delivery history and ordering. No separate model call or timer is introduced.
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field
from typing import Literal

from app.reception_v3.seasonal_quotes import contains_trip_quote

DELIVERED = {'simulated_delivered', 'submitted', 'sent', 'delivered', 'read'}


class LeadReview(BaseModel):
    model_config = ConfigDict(extra='forbid')
    question_message_ids: list[str] = Field(default_factory=list, max_length=100)
    contact_status: Literal['unknown', 'invited', 'provided', 'declined', 'deferred'] = 'unknown'
    contact_message_id: str = ''


def local_day(value):
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(ZoneInfo('Asia/Taipei')).date()


def task(context):
    config = context.get('skills', {}).get('lead_capture', {})
    state, profile = context.get('state', {}), context.get('profile', {})
    if (context.get('event') != 'silence_due' or not config.get('enabled')
            or not config.get('next_day_invitation_enabled')
            or state.get('human_followup') or state.get('handoff') or state.get('opt_out')
            or profile.get('contact_capture_declined') or profile.get('contact_value')
            or profile.get('contact_status') in ('captured', 'self_reported_added', 'provided', 'attachment_pending_verification')):
        return None
    if config.get('require_supported_route', True) and context.get('route_variant') not in context['skills']['routes']:
        return None
    if config.get('require_party_size') and not (profile.get('party_size') or profile.get('party_size_min')):
        return None
    if config.get('require_departure_window') and not profile.get('departure_window'):
        return None
    history = [m for m in context.get('messages', []) if m.get('direction') == 'incoming'
               or m.get('direction') == 'outgoing' and m.get('status') in DELIVERED]
    if any(m.get('direction') == 'outgoing' and m.get('contact_invitation') for m in history):
        return None
    incoming = [m for m in history if m.get('direction') == 'incoming' and m.get('created_at')]
    if not incoming:
        return None
    quotes = [m for m in history if m.get('direction') == 'outgoing' and contains_trip_quote(m.get('content', ''))]
    # A reply delivered after midnight does not become a next-day invitation
    # just because the customer's question arrived the preceding evening.
    anchor = max(local_day(m['created_at']) for m in incoming + quotes if m.get('created_at'))
    return {'next_day': local_day(context['now']) > anchor,
            'question_threshold': config.get('consultation_turn_threshold', 3),
            'quoted_message_ids': [m['id'] for m in quotes if m.get('id')],
            'evidence_messages': [{k: m[k] for k in ('id', 'direction', 'content', 'created_at') if k in m}
                                  for m in history],
            'instruction': '按通用Skill审核有效咨询与既有留资意愿，返回lead_review，引用此处真实消息ID。'
                           '已实际报价或有效咨询达到阈值时，次日首次触达先邀请私讯；同一轮不发洗单素材。'}


def outcome(decision, context):
    """Return ordinary/wait/invite, rejecting invented or repeated evidence."""
    review_task = task(context)
    if not review_task or decision.get('action') == 'handoff' or decision.get('opt_out') is True:
        return 'ordinary'
    raw = decision.get('lead_review')
    if raw is None:
        raise ValueError('本轮需lead_review，先审核历史咨询与联系方式，再决定跟进。')
    review = LeadReview.model_validate(raw)
    evidence = {m['id']: m for m in review_task['evidence_messages'] if m.get('id')}
    ids = review.question_message_ids
    if len(ids) != len(set(ids)) or any(
            key not in evidence or evidence[key]['direction'] != 'incoming'
            or not evidence[key].get('content', '').strip() for key in ids):
        raise ValueError('咨询证据必须为不同的真实客户文字消息ID，不能引用机器人回复、空附件或重复计数。')
    if review.contact_status != 'unknown':
        source = evidence.get(review.contact_message_id)
        direction = 'outgoing' if review.contact_status == 'invited' else 'incoming'
        if not source or source['direction'] != direction:
            raise ValueError('既有邀请、留资、拒绝或预约稍后联系必须引用对应真实消息ID。')
        return 'ordinary'
    qualified = bool(review_task['quoted_message_ids']) or len(ids) >= review_task['question_threshold']
    return ('invite' if review_task['next_day'] else 'wait') if qualified else 'ordinary'


def validation_error(decision, context):
    inviting = any(m.get('purpose') == 'contact_invitation' for m in decision['messages'])
    config = context.get('skills', {}).get('lead_capture', {})
    profile = {**context.get('profile', {}), **decision.get('profile', {})}
    if inviting and (config.get('enabled') is False or profile.get('contact_capture_declined')
                     or profile.get('contact_value') or context.get('state', {}).get('opt_out')
                     or profile.get('contact_status') in ('captured', 'self_reported_added', 'provided', 'attachment_pending_verification')):
        return '当前留资关闭、已留资或客户已拒绝；只处理当前问题，不再邀请私讯。'
    if inviting and context.get('event') == 'silence_due' and (
            any(m.get('contact_invitation') and m.get('direction') == 'outgoing' and m.get('status') in DELIVERED
                for m in context.get('messages', []))
            or (decision.get('lead_review') or {}).get('contact_status', 'unknown') != 'unknown'):
        return '历史已邀请、已留资、拒绝留资或约定稍后联系，本次沉默节点不能重复邀请。'
    try:
        result = outcome(decision, context)
    except ValueError as exc:
        return str(exc)
    if result == 'wait' and (decision['action'] != 'wait' or decision['messages'] or decision['start_introduction']):
        return '已符合次日私讯优先条件，但今天仍是咨询/报价当天。本轮wait，不提前邀请或洗单。'
    if result == 'invite':
        messages = decision['messages']
        if (decision['action'] != 'reply' or decision['start_introduction'] or len(messages) != 1
                or messages[0].get('purpose') != 'contact_invitation'
                or not (messages[0].get('text') or messages[0].get('script_id'))
                or messages[0].get('asset_keys') or messages[0].get('material_rule_id')):
            return '次日首轮仅发送一条自然的私讯邀请：purpose=contact_invitation，不附洗单图文或再介绍。'
    return None
