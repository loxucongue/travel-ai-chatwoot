"""An explicit recommended presentation, without recording customer acceptance."""
INSTRUCTION = '''桃花介绍业务规则（2026-11-22）：客户想了解桃花，或同时想看9日与11日，尚未看过完整介绍且没有明确不上珠峰、只要9日、日期天数不适合等限制时，先给11日完整参考介绍，最后简短说明9日少珠峰段两天；不先追问要哪条或上不上珠峰。
这不是客户已选线：使用action=reply、route_choice=recommend、profile.discussion_route=peach_11d_2027、reference_introduction_route=peach_11d_2027、start_introduction=false，route_variant保留原值。系统发送配置完整图文并附上差异，messages仅放必要承接，不重复图文或差异，不添加另一选线问题。已选11日按原start_introduction流程；已介绍过、客户仅问单项或明确拒绝时不重新介绍。仅缺出发月份不阻止参考介绍，已有明确日期冲突须先处理。'''


INSTRUCTION += '\n参考介绍时messages只简短说先提供11日行程，不另写或概括团期日期，准确团期由配置图文提供。'


def validation_error(decision, context):
    target = decision.get('reference_introduction_route')
    if not target:
        return None
    profile = {**context.get('profile', {}), **decision.get('profile', {})}
    if (context.get('event') != 'customer_message' or decision.get('action') != 'reply'
            or target not in context.get('skills', {}).get('routes', {})
            or decision.get('route_choice') != 'recommend' or profile.get('discussion_route') != target
            or decision.get('start_introduction')
            or (decision.get('route_variant') or '') != (context.get('route_variant') or '')):
        return '参考介绍只用于本轮客户咨询，保持原选线，以recommend讨论已启用桃花11日；不能同时开始正式介绍。'
    from app.reception_v3.route_choice import rejects_current_route
    days = profile.get('trip_days')
    if rejects_current_route(target, profile) or isinstance(days, (int, float)) and days < 11:
        return '客户已知偏好或天数不适合11日，不能强推参考介绍。'
    if any(m.get('status') in ('submitted', 'sent', 'delivered', 'read', 'simulated_delivered')
           and m.get('reference_comparison_route') == target for m in context.get('messages', [])) or target in context.get('state', {}).get('completed_introductions', []):
        return '已有11日介绍记录，直接承接当前问题，不重复整套介绍。'
    return None
