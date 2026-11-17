"""Read-only production diagnostics. Transport attempts and delivered turns are distinct."""
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
import math
from sqlalchemy import select, func
from app.automation_models import AutomationRun as Run, AutomationSession as Session
from app.models import ConversationState as Conversation, InboxBinding, HandoffTask, OutboundMessage
from app.operations import allowed_inbox_ids


def timestamp(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result if result.tzinfo else result.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError, AttributeError):
        return None


def distribution(values):
    values = sorted(v for v in values if isinstance(v, (int, float)) and math.isfinite(v) and v >= 0)
    return {'samples': len(values), 'mean': round(sum(values)/len(values), 2) if values else None,
            **{f'p{p}': round(values[max(0, math.ceil(len(values)*p/100)-1)], 2) if values else None for p in (50, 95)}}


def failure_reason(call):
    status, error = call.get('http_status'), str(call.get('error') or '').lower()
    code = call.get('error_code')
    labels = {'empty_model_response':'返回内容为空', 'model_output_token_limit':'输出长度不足',
              'deepseek_output_token_limit':'输出长度不足', 'incomplete_model_response':'返回不完整',
              'invalid_model_tool_call':'工具调用格式异常', 'invalid_model_response_format':'返回格式异常',
              'incomplete_model_stream':'响应中断', 'incomplete_model_tool_stream':'工具调用不完整'}
    if code in labels: return labels[code]
    if 'timeout' in error: return '请求超时'
    if status in (401, 403): return '鉴权／权限'
    if status == 429: return '限流／额度'
    if isinstance(status, int) and status >= 500: return '厂商服务错误'
    if 'connect' in error or 'network' in error: return '连接失败'
    if isinstance(status, int) and status >= 400: return '请求被拒绝'
    if 'json' in error or 'decode' in error: return '返回格式异常'
    if error == 'readerror': return '响应读取失败（未细分）'
    if error == 'valueerror': return '返回内容异常（未细分）'
    return '其他／原因未记录'


def decoded(value, default):
    if isinstance(value, str):
        try: return json.loads(value)
        except ValueError: return default
    return value if value is not None else default


def read_technical(db, user, start=None, end=None, inbox_id=None, now=None):
    from app.bi_api import window, REASONS
    now = now or datetime.now(timezone.utc)
    begin, finish = window(start, end, now)
    high = min(finish, now)
    scope = select(Conversation.id).join(InboxBinding, Conversation.inbox_binding_id == InboxBinding.id)
    allowed = allowed_inbox_ids(db, user)
    if allowed is not None: scope = scope.where(Conversation.inbox_binding_id.in_(allowed))
    if inbox_id is not None: scope = scope.where(InboxBinding.chatwoot_inbox_id == inbox_id)
    # julianday handles both Z and offset timestamps, including historical formats.
    def period(column):
        return (func.julianday(column) >= func.julianday(begin.isoformat()),
                func.julianday(column) < func.julianday(high.isoformat()))
    rows = list(db.execute(select(Run.id, Run.session_id, Run.module, Run.status, Run.created_at,
        Run.completed_at, func.json_extract(Run.trace, '$.model_http_calls'),
        func.json_extract(Run.input_snapshot, '$.buffered_questions'))
        .join(Session, Run.session_id == Session.id)
        .where(Session.environment == 'live', Session.conversation_state_id.in_(scope), *period(Run.created_at))))
    run_ids = {r.id for r in rows}
    session_ids = {r.session_id for r in rows}
    # Load only message linkage from sessions, never prompts or entire input snapshots.
    parts = {}
    for sid, raw in db.execute(select(Session.id, Session.messages).where(Session.id.in_(session_ids))):
        for part in raw or []:
            if part.get('run_id') in run_ids and part.get('direction') == 'outgoing' and not part.get('private'):
                parts[f"live-v3:{sid}:{part.get('id')}"] = part['run_id']
    first_sent = {}
    delivery = Counter()
    for key, status, submitted in db.execute(select(OutboundMessage.idempotency_key,
            OutboundMessage.status, OutboundMessage.submitted_at).where(
            OutboundMessage.conversation_state_id.in_(scope), OutboundMessage.source_type == 'ai',
            OutboundMessage.source_id.in_(session_ids))):
        rid = parts.get(key)
        if rid is None: continue
        delivery[status] += 1
        at = timestamp(submitted)
        if status in ('submitted', 'sent', 'delivered', 'read') and at and at <= now:
            first_sent[rid] = min(first_sent.get(rid, at), at)
    attempts = {}; missing = 0; latency = []; generated = []; replied = 0; recovered = 0
    for row in rows:
        records = decoded(row[6], None)
        if records is None: missing += 1
        for index, call in enumerate(records or []):
            if isinstance(call, dict): attempts.setdefault(call.get('id') or f'{row.id}:{index}', call)
        if row.module == 'reply' and row.id in first_sent and any(c.get('status') == 'failed' for c in records or []): recovered += 1
        if row.module != 'reply': continue
        if row.id in first_sent:
            replied += 1
            questions = decoded(row[7], [])
            times = [timestamp(q.get('created_at')) for q in questions if isinstance(q, dict) and q.get('direction') == 'incoming']
            times = [t for t in times if t is not None]
            if times:
                elapsed = (first_sent[row.id] - min(times)).total_seconds()
                if elapsed >= 0: latency.append(elapsed)
        a, b = timestamp(row.created_at), timestamp(row.completed_at)
        if row.status == 'completed' and a and b: generated.append((b-a).total_seconds())
    calls = list(attempts.values())
    failures = [c for c in calls if c.get('status') == 'failed']
    finished = [c for c in calls if c.get('status') in ('completed', 'failed')]
    providers = defaultdict(list)
    for c in calls: providers[(str(c.get('provider') or '未记录'), str(c.get('model') or '未记录'))].append(c)
    handoffs = list(db.execute(select(HandoffTask.reason_code, HandoffTask.ai_assist_allowed)
        .where(HandoffTask.conversation_state_id.in_(scope), *period(HandoffTask.created_at))))
    reasons = Counter(REASONS.get(r, '其他／历史原因') for r, _ in handoffs)
    def counts(counter): return [{'name': name, 'value': n} for name, n in counter.most_common()]
    def call_latency(items): return distribution([c['duration_ms']/1000 for c in items if isinstance(c.get('duration_ms'), (float,int))])
    return {
        'as_of': now.isoformat(), 'runs': len(rows), 'missing_call_records': missing,
        'handoffs': {'total': len(handoffs), 'assist_allowed': sum(bool(a) for _, a in handoffs), 'reasons': counts(reasons)},
        'calls': {'total': len(calls), 'failed': len(failures),
                  'failure_rate': round(len(failures)/len(finished)*100, 1) if finished else None,
                  'unfinished': len(calls)-len(finished),
                  'fallback': sum(bool(c.get('fallback_reason')) for c in calls),
                  'recovered_replies': recovered, 'reasons': counts(Counter(failure_reason(c) for c in failures)),
                  'latency': call_latency(calls)},
        'reply': {'sent': replied, 'latency': distribution(latency), 'processing_latency': distribution(generated)},
        'runs_by_status': counts(Counter(r.status for r in rows)), 'delivery': dict(delivery),
        'models': [{'provider': provider, 'model': model, 'calls': len(items),
                    'failed': sum(c.get('status') == 'failed' for c in items), 'latency': call_latency(items)}
                   for (provider, model), items in sorted(providers.items())],
    }
