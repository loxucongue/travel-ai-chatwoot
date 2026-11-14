"""Validate executable reply output, without deciding customer intent."""


def validate_reply_output(decision, context):
    if decision.get('action') != 'reply' or decision.get('start_introduction'):
        return
    if any(str(m.get('text') or '').strip() or m.get('script_id')
           or m.get('quote_options') or m.get('asset_keys') or m.get('material_rule_id')
           for m in decision.get('messages', [])):
        return
    # A running system plan can already own the next outbound message.
    state = context.get('state') or {}
    if state.get('delivery_kind') or state.get('intake_status') in ('sending', 'waiting'):
        return
    raise ValueError(
        'empty_reply_action: reply has no executable content or introduction action. '
        'No separate system question is scheduled. Supply the intended response in messages '
        'or start_introduction=true when appropriate. Use wait only when the conversation '
        'actually calls for silence, not to bypass this correction. Preserve the business facts.')
