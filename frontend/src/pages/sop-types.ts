export type ContentType = 'text' | 'image' | 'video' | 'audio' | 'file';
export const sopReason = (value: string) => ({
  customer_new_message: '客户再次回复，本轮结束', human_or_contact_block: '人工接管或安全标签阻断',
  exit_label: '命中业务退出标签', ai_disabled: 'AI 开关已关闭', channel_cannot_reply: '渠道当前不可回复',
  automatic_window_closed: '超出自动消息窗口', contact_frequency_limit: '跨轮次或策略触达间隔不足',
  outside_contact_hours: '不在 09:00–21:00 联系时段', expired: '计划时间已过期',
  predecessor_not_confirmed: '前一组未确认完成', trusted_customer_message_missing: '缺少可信客户消息',
  material_unavailable: '本地素材不可用', session_reset: '演练已重置', passive_reply_pending: '等待被动回复完成',
  material_revision_changed: '素材文件版本已变化', material_review_required: '素材尚未通过演练审核',
  material_route_mismatch: '素材与线路不匹配', material_binding_changed: '素材绑定已变化',
  material_already_provided: '素材此前已提供，本组跳过', partial_material_duplicate: '部分素材重复，需调整内容组',
  route_changed: '客户线路已变化', test_conversation_required: '不在测试会话范围',
  material_duplicate_requires_review: '素材重复，整组需重新核对',
  contact_state_unknown: '联系人状态待核实', customer_added_time_missing: '尚无客户首次进入时间',
  human_replied: '人工已回复，本轮停止', ai_opt_in_required: '会话缺少 ai 接管标签',
  human_handoff_active: '存在人工接管任务', submission_unknown_reconcile_required: '发送结果未知，需人工核对',
  previous_submission_failed: '该内容此前发送失败，不自动重试', material_not_approved_for_live: '素材尚未批准用于真实发送',
  live_scope_mismatch: '不在真实测试账号范围', sop_not_running: '策略已暂停或停止',
} as Record<string, string>)[value] ?? value;
export interface SopContent { key: string; content_type: ContentType; content: string; media_id?: number; media_name?: string; asset_key?: string; media_hash?: string }
export interface NodeItem {
  skip_if_materials_provided?: boolean;
  key: string;
  schedule_type: 'relative' | 'calendar_day' | 'fixed';
  basis: 'customer_added' | 'previous_node' | 'enrollment' | 'last_customer_reply';
  delay_minutes?: number;
  day_number?: number;
  time_of_day?: string;
  fixed_at?: string;
  messages: SopContent[];
  content_type?: ContentType;
  content?: string;
  media_id?: number;
}
export interface Sop {
  route_variant: string; test_conversation_ids: number[];
  version: number; id: number; name: string; description: string; status: string; dry_run: boolean; live_enabled: boolean;
  trigger_type: string; trigger_labels: string[]; inbox_ids: number[]; nodes: NodeItem[]; exit_labels: string[];
  stop_on_incoming: boolean; frequency_hours: number; enrolled: number; sent: number; blocked: number; updated_at: string;
  rehearsal_enrolled: number; rehearsal_sent: number; rehearsal_blocked: number; published_version: number | null; has_unpublished_changes: boolean;
  published_nodes?: NodeItem[]; published_at?: string | null;
  live_enrolled: number; live_sent: number; live_blocked: number;
}
export interface SopForm {
  route_variant: string; test_conversation_ids: number[];
  time_anchor: 'customer_added' | 'enrollment';
  inbox_ids: number[]; name: string; description: string; trigger_type: string; trigger_labels: string;
  exit_labels: string; stop_on_incoming: boolean; frequency_hours: number; dry_run: boolean; live_enabled: boolean; nodes: NodeItem[];
}
export const newContent = (content_type: ContentType = 'text'): SopContent => ({ key: crypto.randomUUID(), content_type, content: '' });
export const emptyNode = (): NodeItem => ({ key: crypto.randomUUID(), schedule_type: 'relative', basis: 'enrollment', delay_minutes: 10, day_number: 1, time_of_day: '10:00', messages: [newContent()] });
export const emptyForm = (): SopForm => ({ route_variant: '', test_conversation_ids: [], time_anchor: 'enrollment', inbox_ids: [], name: '', description: '', trigger_type: 'manual', trigger_labels: '', exit_labels: '', stop_on_incoming: true, frequency_hours: 24, dry_run: true, live_enabled: false, nodes: [emptyNode()] });
export function normalizeNode(node: NodeItem): NodeItem {
  return { ...node, day_number: node.day_number ?? 1, time_of_day: node.time_of_day ?? '10:00', messages: node.messages ?? [{ key: `${node.key}_content`, content_type: node.content_type ?? 'text', content: node.content ?? '', media_id: node.media_id }] };
}
export const contentLabel = (type: ContentType) => ({ text: '文字', image: '图片', video: '视频', audio: '音频', file: '文件' })[type];
export function timingLabel(node: NodeItem): string {
  if (node.schedule_type === 'calendar_day') return `${node.basis === 'enrollment' ? '入组' : '首次进入'}后第 ${node.day_number ?? 1} 天 ${node.time_of_day ?? '10:00'}`;
  if (node.schedule_type === 'fixed') return node.fixed_at ? new Date(node.fixed_at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' }) : '固定日期未设置';
  const base = { customer_added: '客户添加', previous_node: '上一组发送', enrollment: '入组', last_customer_reply: '客户最后回复' }[node.basis];
  return `${base}后 ${node.delay_minutes ?? 0} 分钟`;
}
