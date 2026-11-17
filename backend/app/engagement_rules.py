"""Engagement starts after an actual introduction delivery, never a draft."""
from app.runtime_settings import dt

ACCEPTED = {'submitted', 'sent', 'delivered', 'read'}
ENTRY = {'getstarted', '開始使用', '开始使用', '開始', '开始'}


def boundary(session, outbounds, legacy_texts=()):
    if not session or session.environment != 'live':
        return None
    lookup = {o.idempotency_key: o for o in outbounds}
    candidates = []
    for p in session.messages or []:
        # Historic introduction plans mixed greetings with route content. Only
        # explicitly marked route content is authoritative for new sessions.
        legacy = (p.get('v3_kind') == 'introduction' and p.get('introduction_content') is None
                  and p.get('content', '').strip() in legacy_texts)
        if p.get('introduction_content') is not True and not legacy:
            continue
        o = lookup.get(f'live-v3:{session.id}:{p.get("id")}')
        if o and o.status in ACCEPTED and o.submitted_at:
            candidates.append({'at': o.submitted_at, 'message_id': o.chatwoot_message_id,
                               'outbound_id': o.id, 'proof': 'legacy_catalog_exact' if legacy else 'live_outbox'})
    return min(candidates, key=lambda p: dt(p['at']), default=None)


def eligible(message, start, is_option=False):
    if (not start or message.direction != 'incoming' or message.private or is_option
            or (message.content_attributes or {}).get('external_echo')
            or ''.join((message.content or '').split()).casefold() in ENTRY
            or not ((message.content or '').strip() or message.attachments)):
        return False
    # Remote IDs order messages even when timestamp precision is only seconds.
    if start.get('message_id') and message.chatwoot_message_id:
        return message.chatwoot_message_id > start['message_id']
    return dt(message.created_at) > dt(start['at'])


def introduction_boundary(db, conversation_id):
    from sqlalchemy import select
    from app.automation_models import AutomationSession
    from app.models import OutboundMessage
    session = db.scalar(select(AutomationSession).where(
        AutomationSession.conversation_state_id == conversation_id, AutomationSession.environment == 'live'))
    outbounds = list(db.scalars(select(OutboundMessage).where(OutboundMessage.conversation_state_id == conversation_id)))
    return session, boundary(session, outbounds, catalog_texts(db, session))


def catalog_texts(db, session):
    if not session or not any(p.get('v3_kind') == 'introduction' and p.get('introduction_content') is None
                              and p.get('content') for p in session.messages or []):
        return set()
    from app.reception_v3.skills import compile_skills
    from app.reception_v3.seasonal_quotes import seasonal_group
    from app.reception_v3.message_chunks import text_chunks
    bundle = compile_skills(db)
    return {chunk.strip()
            for r in bundle['routes'].values() for key in r.get('introduction_sequence', [])
            if key in r.get('groups', {}) and r['groups'][key].get('text')
            for chunk in text_chunks(seasonal_group(r['groups'][key], session.memory or {})['text'])}
