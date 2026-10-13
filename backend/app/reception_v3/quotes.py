"""Customer quote validity, independent of model memory and conversation resets."""
from copy import deepcopy
from datetime import datetime
import hashlib
import json

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert

from app.automation_models import CustomerQuoteOffer
from app.models import ConversationState
from app.reception_v3.skills import effective_price_scripts


def subject_key(db, row):
    if row.environment == 'live':
        conversation = db.get(ConversationState, row.conversation_state_id)
        if conversation and conversation.contact_id:
            return f'contact:{conversation.tenant_id}:{conversation.contact_id}'
        return f'conversation:{row.conversation_state_id}'
    return f'playground:{row.id}'


def get_offer(db, row, route):
    record = db.scalar(select(CustomerQuoteOffer).where(
        CustomerQuoteOffer.subject_key == subject_key(db, row), CustomerQuoteOffer.route_variant == route))
    return deepcopy(record.offer) if record else None


def personalize_quotes(db, row, bundle):
    if not any(r.get('quote_policy') for r in bundle['routes'].values()):
        return bundle
    bundle = deepcopy(bundle)
    now = datetime.fromisoformat(row.virtual_now.replace('Z', '+00:00'))
    for route, spec in bundle['routes'].items():
        if not spec.get('quote_policy'):
            continue
        price = effective_price_scripts([spec['quote_policy']], now, get_offer(db, row, route))[0]
        spec['scripts'] = [price if s['id'] == 'price' else s for s in spec['scripts']]
        for fact in spec['facts']:
            if fact['id'] == route + '.current_quote':
                fact['text'] = '本轮当前有效报价（优先于历史价格）：\n' + price['answer_text']
        for key in ('price_reference', 'price_deferral'):
            if key in spec['groups']:
                spec['groups'][key]['text'] = price['answer_text']
    bundle['digest'] = hashlib.sha256(json.dumps(bundle, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return bundle


def reserve_quoted_offers(db, row, bundle, parts):
    """Only an actual quote plan starts validity, never loading a Skill or introduction.

    Unique customer+route makes simultaneous conversations use the same promise.
    Reservation and the outgoing plan commit together. Retries cannot extend it.
    """
    for route, spec in bundle['routes'].items():
        policy = spec.get('quote_policy')
        price = next((s for s in spec.get('scripts', []) if s['id'] == 'price'), None)
        if not policy or not price or not price.get('quote_offer'):
            continue
        matching = [part for part in parts if price['answer_text'] in part.get('content', '')]
        if not matching:
            continue
        db.execute(insert(CustomerQuoteOffer).values(subject_key=subject_key(db, row), route_variant=route,
            offer=price['quote_offer']).on_conflict_do_nothing(index_elements=['subject_key', 'route_variant']))
        promise = get_offer(db, row, route)
        canonical = effective_price_scripts([policy], datetime.fromisoformat(row.virtual_now.replace('Z', '+00:00')), promise)[0]
        for part in matching:
            part['content'] = part['content'].replace(price['answer_text'], canonical['answer_text'])
            part['quote_offer'] = {'route_variant': route, **promise}
