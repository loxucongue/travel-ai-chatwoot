"""Project shared facts and real operational state, never another engine's inference."""
from copy import deepcopy
from sqlalchemy import select
from app.models import ConversationJourney, HandoffTask
from app.lead_capture_models import LeadCaptureState


def project_engine_state(db, state, previous: str, target: str, now: str) -> dict:
    journey = db.scalar(select(ConversationJourney).where(ConversationJourney.conversation_state_id == state.id))
    if not journey:
        return {}
    slots = deepcopy(journey.slots or {})
    stages = dict(slots.get('_engine_stage_history') or {})
    stages[previous] = {'stage': journey.stage, 'at': now, 'journey_version': journey.version}
    slots['_engine_stage_history'] = stages
    # Captured and handoff require independent persisted evidence.
    handoff = db.scalar(select(HandoffTask.id).where(HandoffTask.conversation_state_id == state.id,
                         HandoffTask.status.in_(['pending', 'claimed'])))
    lead = db.scalar(select(LeadCaptureState).where(LeadCaptureState.conversation_state_id == state.id))
    stage = ('handoff' if handoff else 'captured' if lead and lead.status == 'captured'
             else 'value_building' if journey.route_variant else 'route_selection')
    old_stage = journey.stage
    journey.stage, journey.slots = stage, slots
    journey.version += 1
    journey.updated_at = now
    return {'from_stage': old_stage, 'to_stage': stage, 'facts_and_receipts_preserved': True}
