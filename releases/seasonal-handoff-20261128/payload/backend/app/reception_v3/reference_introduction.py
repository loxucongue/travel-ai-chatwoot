"""An explicit recommended presentation, without recording customer acceptance."""
import re

ACTION_INSTRUCTION = ('本轮若承诺现在提供完整线路介绍，必须同时生成start_introduction或'
    'reference_introduction_route对应的介绍动作；不能仅发送承诺文字。'
    '普通单项答疑不触发整套介绍；已介绍、客户拒绝或条件不适合时直接承接问题，不承诺重发。')


def promised_introduction_without_action(decision):
    """Catch a present-tense full-presentation promise, not generic travel advice."""
    if decision.get('action') != 'reply' or decision.get('start_introduction') or decision.get('reference_introduction_route'):
        return False
    for message in decision.get('messages', []):
        text = message.get('text') or ''
        for sentence in re.split(r'[。！？!?\n；;]', text):
            if re.search(r'如果|若|之後|之后|稍後|稍后|可以|要不要|是否|已經|已经|上面|剛剛|刚刚', sentence):
                continue
            if (re.search(r'(?:先|現在|现在|接下來|接下来)?(?:給您|给您|給你|给你|為您|为您|幫您|帮您).{0,8}(?:看|發|发|提供|介紹|介绍)', sentence)
                    and re.search(r'完整(?:的)?(?:安排|介紹|介绍|行程)|(?:10|11|15|十|十一|十五)[日天].{0,12}(?:安排|介紹|介绍|行程)|這條的安排|这条的安排|給您看行程|给您看行程', sentence)):
                return True
    return False
INSTRUCTION = '''桃花介绍业务规则（2026-11-22）：客户想了解桃花，或同时想看9日与11日，尚未看过完整介绍且没有明确不上珠峰、只要9日、日期天数不适合等限制时，先给11日完整参考介绍，最后简短说明9日少珠峰段两天；不先追问要哪条或上不上珠峰。
这不是客户已选线：使用action=reply、route_choice=recommend、profile.discussion_route=peach_11d_2027、reference_introduction_route=peach_11d_2027、start_introduction=false，route_variant保留原值。系统发送配置完整图文并附上差异，messages仅放必要承接，不重复图文或差异，不添加另一选线问题。已选11日按原start_introduction流程；已介绍过、客户仅问单项或明确拒绝时不重新介绍。仅缺出发月份不阻止参考介绍，已有明确日期冲突须先处理。'''


INSTRUCTION += '\n桃花参考介绍时messages只简短说先提供11日行程，不另写或概括团期日期，准确团期由配置图文提供。'

SEASONAL_INSTRUCTION = '''季节不适合时的接待规则（2026-11-28）：
仅在正在讨论西藏相关行程时使用。客户桃花日期晚于产品最晚出发日，解释会错过这趟行程安排的赏花时段，2027桃花最晚4月10日出发，问一次是否愿意提前；不要只说不在范围/区间，也不要列出多个备选。4月10日不是所有地区桃花消失的日期；花况随天气变化，不保证观赏。
客户已明确不能改期，不再反复问改期。直接承接西藏全览10日作为一个参考方向，先load_skill查看资料。日期不匹配不阻止参考图文：例如4月15日没有开放全览团期，仍先介绍经典线路吸引兴趣，简短说明原日期尚不能确认、喜欢后可协调其他日期或请顾问协助；不要只报限制后结束或立即转人工。询问原日期能否成行或报价才用get_departure_quote核对，不能把参考介绍说成原日期有团。可以自然邀请LINE/微信/WhatsApp传详细资料，已留资或拒绝联系不再索取。其他已知时长、珠峰偏好冲突先解释，不强推客户明确拒绝的安排。
蓝冰客户超出冬季产品开放时段，解释不是这条行程适合看冬季蓝冰的季节，不能承诺看到蓝冰；同样优先全览10日，不罗列备选，不把冰川本身说成不存在。不能把冬季蓝冰线路推荐给4月客户。
客户在常规西藏景点的上下文明确说这些都去过了，优先阿里15日作为深入游方向，不反复介绍原线路；北京等其他目的地去过不触发此规则。已说阿里也去过、不愿去或日期/时长/体能不适合时，不强推，保留阿里年龄体能核对要求。
适用且未介绍过的全览10日或阿里15日，可直接提供完整参考介绍：action=reply，route_choice=recommend，profile.discussion_route与reference_introduction_route填目标线路，route_variant保持原选线，start_introduction=false。必须先load_skill加载目标，不能把推荐记成客户选择。不适合或已经发过时不要生成重复介绍，先回答当前问题。'''
SEASONAL_INSTRUCTION += '''
业务明确允许为了吸引改期而介绍原日期不开放的线路。不要因为全览10日原日期无团就另推精华8日等日期匹配产品。不能改期、只想参考经典西藏的客人，直接生成全览10日reference_introduction_route；不要再问「想了解哪条」。参考介绍不能同时报价或声称原日期可出发。天数不足可以先简短介绍阿里价值，再问能否增加天数，不列云南/北京等备选清单。'''


def validation_error(decision, context):
    latest = next((m.get('content','') for m in reversed(context.get('messages',[])) if m.get('direction')=='incoming'), '')
    if (decision.get('start_introduction') and decision.get('route_variant') == 'tibet_ali_15d'
            and context.get('route_variant') != 'tibet_ali_15d'
            and re.search(r'都去過|都去过|全去過|全去过', latest)
            and '阿里' not in latest):
        return '客户只说常规景点都去过，并未选择阿里。以recommend和reference_introduction_route提供阿里参考，保留原选线。'
    if promised_introduction_without_action(decision):
        return ACTION_INSTRUCTION + '尚未发送，请补齐对应动作；如不应介绍，请改为真实可执行的回复。'
    target = decision.get('reference_introduction_route')
    if not target:
        return None
    profile = {**context.get('profile', {}), **decision.get('profile', {})}
    if (context.get('event') != 'customer_message' or decision.get('action') != 'reply'
            or target not in context.get('skills', {}).get('routes', {})
            or decision.get('route_choice') != 'recommend' or profile.get('discussion_route') != target
            or decision.get('start_introduction')
            or (decision.get('route_variant') or '') != (context.get('route_variant') or '')):
        return '参考介绍只用于本轮客户咨询，保持原选线，以recommend讨论已启用目标线路；不能同时开始正式介绍。'
    from app.reception_v3.route_choice import rejects_current_route
    days = profile.get('trip_days')
    required_days = {'peach_11d_2027':11, 'tibet_overview_10d':10, 'tibet_ali_15d':15}[target]
    if rejects_current_route(target, profile) or isinstance(days, (int, float)) and days < required_days:
        return '客户已知偏好或天数不适合目标线路，不能强推参考介绍。'
    if any(m.get('status') in ('submitted', 'sent', 'delivered', 'read', 'simulated_delivered')
           and (m.get('reference_comparison_route') == target or m.get('reference_introduction_route') == target) for m in context.get('messages', [])) or target in context.get('state', {}).get('completed_introductions', []):
        return '已有目标线路介绍记录，直接承接当前问题，不重复整套介绍。'
    return None
