"""Execute configured labels from model-interpreted customer facts."""


def labels_for_profile(profile, route, routes=None, updates=None):
    """Resolve explicit model facts without selecting a standard itinerary.

    A route-exclusive field can label a custom enquiry with no selected route.
    Only this turn's updates qualify; stale profile fields cannot re-tag it.
    Shared field names remain scoped to the selected route.
    """
    labels = []
    for field, values in route.get('profile_labels', {}).items():
        value = profile.get(field)
        if isinstance(value, str):
            labels.extend(values.get(value, []))
    for field, value in (updates or {}).items():
        if not isinstance(value, str):
            continue
        owners = [spec['profile_labels'][field] for spec in (routes or {}).values()
                  if field in spec.get('profile_labels', {})]
        if len(owners) == 1:
            labels.extend(owners[0].get(value, []))
    return list(dict.fromkeys(labels))


def apply(db, row, route, *, routes=None, updates=None):
    labels = labels_for_profile(row.memory or {}, route, routes, updates)
    labels = list(dict.fromkeys(labels))
    if not labels:
        return
    if row.environment != 'live':
        row.controls = {**row.controls, 'evaluation_labels': list(dict.fromkeys([
            *row.controls.get('evaluation_labels', []), *labels]))}
        return
    from sqlalchemy import select
    from app.models import ConversationLabelAddition, MessageEvent
    source = db.scalar(select(MessageEvent.id).where(
        MessageEvent.conversation_state_id == row.conversation_state_id,
        MessageEvent.direction == 'incoming', MessageEvent.private.is_(False)
    ).order_by(MessageEvent.id.desc()).limit(1))
    if source:
        for label in labels:
            if not db.get(ConversationLabelAddition, (row.conversation_state_id, label)):
                db.add(ConversationLabelAddition(conversation_state_id=row.conversation_state_id,
                    label=label, source_message_id=source))
