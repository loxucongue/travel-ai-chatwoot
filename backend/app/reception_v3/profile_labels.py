"""Execute configured labels from model-interpreted customer facts."""


def apply(db, row, route):
    labels = []
    for field, values in route.get('profile_labels', {}).items():
        value = (row.memory or {}).get(field)
        if isinstance(value, str):
            labels.extend(values.get(value, []))
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
