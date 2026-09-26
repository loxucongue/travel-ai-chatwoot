"""Stable operator categories; raw error codes remain in the trace."""
def classify_reply_failure(code):
    value = str(code or '').lower()
    if 'verification_failed' in value or 'unsupported_claim' in value or 'reviewed_delivery_plan_changed' in value:
        return 'ai_factual_verification_failed', '回复未通过事实、切题或交付核验，请检查本轮问题'
    if any(word in value for word in ('knowledge', 'material_unavailable', 'itinerary_unavailable')):
        return 'knowledge_confirmation_required', '所需知识或批准资料不足，请核对并补充'
    if any(word in value for word in ('timeout', 'connect', 'http_', 'remoteprotocol', 'readerror')):
        return 'ai_service_unavailable', '模型请求未完成，请接续处理客户本轮问题'
    if any(word in value for word in ('invalid', 'missing', 'schema', 'format', 'parse')):
        return 'ai_decision_invalid', '回复决策格式或证据不完整，请接续处理'
    if any(word in value for word in ('submission', 'delivery', 'channel_send', 'chatwoot')):
        return 'ai_delivery_failed', '消息交付未确认，请先核对已发送记录，避免重复发送'
    return 'ai_processing_failed', '自动接待处理失败，请检查本轮问题和原始错误'
