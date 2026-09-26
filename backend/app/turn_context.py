"""Shared bounded history for generation and its independent reviewers."""
def conversation_snapshot(context):
    return [{'id': m.get('id'), 'role': m.get('role') or m.get('direction'),
             'content': str(m.get('content') or '')[:4000]}
            for m in (context.get('context_messages') or [])[-30:]]
