"""Model-selected business checkpoints on the existing durable follow-up clock."""
from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo


def validate(policy, groups):
    if (not isinstance(policy, dict) or type(policy.get('enabled')) is not bool
            or not isinstance(policy.get('checkpoints'), dict) or not policy['checkpoints']
            or any(not isinstance(k,str) or not k or type(v) is not int or not 1 <= v <= 10080
                   for k,v in policy['checkpoints'].items())):
        raise ValueError('route_followup_invalid')
    if any(key not in groups for key in policy.get('daily_sequence', [])):
        raise ValueError('route_followup_group_missing')
    if any(key not in policy['checkpoints'] for key in policy.get('reference_preview_checkpoints', [])):
        raise ValueError('route_followup_preview_checkpoint_invalid')
    for field in ('lead_priority_override', 'allow_repeat_invitation'):
        if field in policy and type(policy[field]) is not bool:
            raise ValueError('route_followup_flag_invalid')


def policy_for(bundle, route, profile):
    from app.reception_v3.large_party import policy_for as group_policy
    large = group_policy(bundle, route, profile)
    if not route:
        large = next((p for key in bundle.get('routes', {}) if (p := group_policy(bundle,key,profile))), None)
    policy = (large or {}).get('followup_policy') or bundle.get('routes',{}).get(route,{}).get('followup_policy')
    return deepcopy(policy) if policy and policy.get('enabled') else None


def task(context):
    policy = policy_for(context['skills'], context.get('route_variant',''), context.get('profile',{}))
    if not policy:
        return None
    outgoing = [m for m in context.get('messages',[]) if m.get('direction') == 'outgoing'
                and m.get('status') in ('simulated_delivered','submitted','sent','delivered','read')]
    stamp = context.get('now')
    local_now = datetime.fromisoformat(stamp.replace('Z','+00:00')).astimezone(ZoneInfo('Asia/Taipei')).isoformat() if stamp else None
    return {'policy':policy,'checkpoint':context.get('state',{}).get('route_checkpoint',''),
            'business_local_now':local_now,'business_timezone':'Asia/Taipei',
            'last_outgoing_read': bool(outgoing and outgoing[-1].get('status') == 'read'),
            'instruction':'followup_checkpoint請填本輪處理完成後要等待的下一階段，不是複製剛觸發的checkpoint。由模型按實際對話選擇；沒有回覆/已讀不等於客戶同意。沒有已讀回執一律未知；人工接手、停用、拒絕、渠道窗口優先。沉默任務已通過系統的時段與每日發送上限檢查；日期以business_local_now為準，不把UTC時間的昨日訊息算作今天而再次攔截。仍按客戶意願及有無適合的新內容決定是否發送。'}


def reference_preview_allowed(context, decision=None):
    """A configured follow-up may offer a map, but cannot select a product."""
    policy = policy_for(context['skills'],context.get('route_variant',''),context.get('profile',{}))
    permitted = (context.get('event') == 'silence_due' and policy
        and context.get('state',{}).get('route_checkpoint') in policy.get('reference_preview_checkpoints',[]))
    if not permitted or decision is None:
        return bool(permitted)
    return (decision.get('route_choice') == 'recommend' and not decision.get('start_introduction')
        and (decision.get('route_variant') or context.get('route_variant','')) == context.get('route_variant',''))


def validation_error(decision, context):
    key = decision.get('followup_checkpoint','')
    profile = {**context.get('profile',{}), **decision.get('profile',{})}
    route = decision.get('route_variant') or context.get('route_variant','')
    policy = policy_for(context['skills'],route,profile)
    if not key:
        if (policy and decision.get('action') not in ('handoff','queue')
                and not any(decision.get(k) for k in ('start_introduction','stop_followup','opt_out','notify_contact'))
                and not context.get('state',{}).get('lead_assist')):
            import json
            return ('本輪適用專屬跟進配置。請保留客戶答覆，按對話選下一階段，將followup_checkpoint作為JSON頂層字段輸出；'
                    '不能只寫進reason或next_check_minutes。配置：'+json.dumps(policy,ensure_ascii=False))
        return None
    if not policy or key not in policy['checkpoints']:
        return 'followup_checkpoint僅能引用當前線路/獨團配置中的檢查點；沒有適用配置請留空。'
    return None


def apply(value, decision, bundle, route, profile):
    policy = policy_for(bundle,route,profile)
    if (not policy or decision.get('stop_followup') or value.get('opt_out') or value.get('handoff')
            or value.get('lead_assist')):
        value.pop('route_checkpoint',None)
        return None
    key = decision.get('followup_checkpoint','')
    if key:
        if key not in policy['checkpoints']:
            raise ValueError('route_checkpoint_not_validated')
        value['route_checkpoint'] = key
        return policy['checkpoints'][key]
    value.pop('route_checkpoint',None)
    return None
