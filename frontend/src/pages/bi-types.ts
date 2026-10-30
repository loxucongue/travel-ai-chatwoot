export interface Metric { key: string; label: string; value: number; rate?: number | null }
export interface BiReport {
  meta: { start: string; end: string; mode: string; as_of: string; refreshed_at: string | null; remaining: number; covered: number; conversations: number; incomplete_history: number; unknown_birth: number; current_added: number; added_time_unknown: number; unlinked_customers: number; population: number; all_customers: number };
  inboxes: { id: number; name: string }[]; metrics: Metric[]; progress: Metric[];
  handoffs: { customers: number; tasks: number; pending: number; overdue: number; answered: number; mean_reply_minutes: number | null; unassigned: number; assigned_unanswered: number; reasons: { name: string; value: number }[] };
  followup: { customers: number; rounds: number; delivery: Record<string, number>; modes: Record<string, number>; returned: number; meaningful_return: number; leads: number; matured_customers: number; matured_replied: number; response_rate: number | null; observing: number; shared_rounds: number; queue: Record<string, number> };
  ai_value: { ai_only: number; mixed: number; captured_with_ai: number; failed_customers: number };
  trends: { day: string; new: number; engaged: number; captured: number; returned: number }[];
}
export interface BiCustomers {
  total: number; page: number; page_size: number; label: string;
  items: { customer_key: string; name: string; conversation_id: number; state: string; advisor: string | null; route: string; at: string; next_followup_at: string | null;
    evidence: { kind: string; at: string; source: string; message_id?: number; task_id?: number; status?: string; shared?: boolean; label?: string; historical_incomplete?: boolean }[] }[];
}
export function localDay(offset = 0, now = new Date()) {
  const d = new Date(now.getTime() + 8 * 3600000 + offset * 86400000);
  return d.toISOString().slice(0, 10);
}
export function validRange(start: string, end: string, today = localDay()) {
  const valid = (s: string) => /^\d{4}-\d{2}-\d{2}$/.test(s) && !Number.isNaN(Date.parse(s)) && new Date(s).toISOString().slice(0, 10) === s;
  return valid(start) && valid(end) && start <= end && end <= today && Date.parse(end) - Date.parse(start) <= 365 * 86400000;
}
export const metricHelp: Record<string, string> = {
  new: '按已关联客户去重，首次进入当前可见渠道范围的日期。历史缺少创建时间时采用首次可见客户消息。',
  ai: '有正式 AI 消息被渠道接受提交的客户；不把未发送草稿、失败消息或演练计入。提交不等于送达。',
  engaged: '客户发送了有效内容，排除开场选项及重复广告入口。点赞属于回应，单独区分于有效咨询。',
  deep: '已有报价，或至少三条被现有接待流程识别且可追溯的有效咨询；不是简单数三条消息。',
  invited: '实际提交过邀请私讯的消息。邀请不代表客户已经提供联系方式。',
  captured: '已记录联系方式交接或业务留资标签。图片待识别、口头说已加好友分别统计。',
  added: '有时间依据的“加私訊”等确认标签记录；新标签按系统收到变化的时间计入。历史仅有当前标签、缺少时间的客户另列，不猜测日期。',
  returned: '跟进提交后24小时内，客户首次有效回复；排除广告点击，可包含点赞。属于时序关联，不等于 AI 单独促成。',
};
export const deliveryLabels: Record<string, string> = { submitted: '已提交·待回执', sent: '已提交·待回执', delivered: '已送达', read: '已读', failed: '失败', partial: '部分失败', unknown: '状态未知' };
export const stateLabels: Record<string, string> = { AI_ACTIVE: 'AI接待', AI_FOLLOWUP: 'AI跟进', HUMAN_HANDOFF: '人工接管', AI_PAUSED_CONVERSATION: '已暂停', AI_PAUSED_INBOX: '渠道已暂停' };
