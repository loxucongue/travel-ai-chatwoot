"""One-shot alternative reference on the existing durable silence clock."""
from datetime import timedelta
from app.reception_v3.post_quote import stamp, DELIVERED
from app.reception_v3.route_choice import PEACH_ROUTES, rejects_current_route


def eligible(value, profile, messages):
    pending = value.get('alternative_pending')
    if not pending or any(value.get(k) for k in ('human_followup','handoff','opt_out','lead_assist')):
        return False
    parts=[m for m in messages if pending.get('plan_id') and m.get('plan_id')==pending['plan_id']]
    if parts and not all(m.get('status') in DELIVERED for m in parts):
        return False
    if (profile.get('contact_capture_declined') or profile.get('contact_value')
            or profile.get('contact_status') in ('captured','provided','self_reported_added','attachment_pending_verification')
            or rejects_current_route(pending['route'], profile)):
        return False
    if any(m.get('direction') == 'outgoing' and m.get('status') in DELIVERED and m.get('contact_invitation')
           and m.get('created_at') and stamp(m['created_at']) >= stamp(pending['offered_at']) for m in messages):
        return False
    return not any(m.get('direction') == 'incoming' and m.get('created_at')
                   and stamp(m['created_at']) > stamp(pending['offered_at']) for m in messages)


def arm(value, profile, messages, plan_id, now):
    if value.get('alternative_last_plan') == plan_id:
        return False
    parts = [m for m in messages if m.get('plan_id') == plan_id]
    offer = next((m for m in parts if m.get('alternative_offer_route')), None)
    if not offer or not all(m.get('status') in DELIVERED for m in parts):
        return False
    pending = {'plan_id':plan_id,'route':offer['alternative_offer_route'],
               'source_message_id':offer['alternative_source_message_id'],
               'offered_at':now,'due_at':(stamp(now)+timedelta(minutes=60)).isoformat(),
               'reason':'customer_peach_interest'}
    candidate = {**value,'alternative_pending':pending}
    if not eligible(candidate,profile,messages):
        return False
    value.update(alternative_pending=pending,alternative_last_plan=plan_id)
    schedule(value)
    return True


def schedule(value):
    pending=value.get('alternative_pending')
    if pending and not value.get('post_quote_pending'):
        value.update(next_check_at=pending['due_at'],next_check_minutes=60,appointment_pending=True)


def task(context):
    value=context.get('state',{})
    pending=value.get('alternative_pending')
    if (context.get('event')!='silence_due' or value.get('post_quote_pending') or not pending
            or not context.get('now') or stamp(context['now']) < stamp(pending['due_at'])
            or not eligible(value,context.get('profile',{}),context.get('messages',[]))):
        return None
    return {**pending,'instruction':'客户表达桃花兴趣后，一小时未回复。确认目标线路适合时只写一句参考介绍承接，程序会接续该线路尚未发送的行程、景点、车辆住宿图文，跳过开场、价格和联系邀请；无需自行列完整图文。route_choice=recommend、profile.discussion_route为目标线路，start_introduction=false，不改变客户已选主线。不再问要不要看。若最新日期/天数或拒绝记录不适用则wait，不强推。followup_checkpoint留空。'}


def validation_error(decision, context):
    for m in decision.get('messages',[]):
        route=m.get('alternative_offer_route')
        if not route:
            continue
        source=next((x for x in context.get('messages',[]) if x.get('id')==m.get('alternative_source_message_id')
                     and x.get('direction')=='incoming' and not x.get('private')),None)
        if (context.get('event')!='customer_message' or route not in PEACH_ROUTES
                or route not in context.get('skills',{}).get('routes',{}) or not source
                or '桃花' not in source.get('content','')):
            return '一小时参考介绍须引用客户实际桃花兴趣消息，不能仅凭我方推荐或沉默建立任务。'
    pending=task(context)
    if pending and decision.get('action')!='wait':
        if (decision.get('route_choice')!='recommend' or decision.get('start_introduction')
                or decision.get('profile',{}).get('discussion_route')!=pending['route']
                or decision.get('followup_checkpoint')
                or any(m.get('purpose')=='contact_invitation' or m.get('quote_options') for m in decision.get('messages',[]))):
            return '本轮只提供目标桃花线路参考介绍，不选线、不报价、不索资、不另建线路节点；不适合则wait。'
    return None


def consume(value):
    pending=value.pop('alternative_pending',None)
    if pending:
        value['alternative_last_result']={**pending,'status':'reviewed'}


def reference_part_allowed(part, bundle, profile):
    """Revalidate program-rendered reference parts against current product config."""
    route=part.get('reference_introduction_route')
    spec=bundle.get('routes',{}).get(route,{})
    key=part.get('reference_group')
    if (not route or key not in spec.get('introduction_sequence',[])
            or rejects_current_route(route,profile)):
        return False
    from app.reception_v3.seasonal_quotes import seasonal_group
    group=seasonal_group(spec['groups'][key],profile)
    if part.get('content_type')=='image':
        return part.get('asset_key') in group['assets']
    return part.get('content')==group['text']
