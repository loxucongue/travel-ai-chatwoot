type JsonRecord = Record<string, unknown>;

const now = Date.now();
const isoAgo = (minutes: number) => new Date(now - minutes * 60_000).toISOString();
const isoAfter = (minutes: number) => new Date(now + minutes * 60_000).toISOString();
const clone = <T>(value: T): T => structuredClone(value);

const agents = [
  { id: 11, name: '王顾问', email: 'wang@example.test', availability_status: 'online', role: 'agent' },
  { id: 12, name: '陈主管', email: 'chen@example.test', availability_status: 'online', role: 'supervisor' },
  { id: 13, name: '林顾问', email: 'lin@example.test', availability_status: 'offline', role: 'agent' },
];

const labelDefinitions = [
  { id: 1, title: 'ai', description: '允许 AI 接管当前会话', color: '#14866D', show_on_sidebar: true },
  { id: 2, title: '人工接管', description: '暂停 AI 并进入人工队列', color: '#DC2626', show_on_sidebar: true },
  { id: 3, title: '方案介绍', description: '已进入方案介绍阶段', color: '#2563EB', show_on_sidebar: true },
  { id: 4, title: '报价中', description: '正在确认报价', color: '#D97706', show_on_sidebar: true },
  { id: 5, title: '已留资', description: '已取得联系方式', color: '#16A34A', show_on_sidebar: true },
  { id: 6, title: '客诉', description: '投诉或高风险咨询', color: '#BE123C', show_on_sidebar: true },
  { id: 7, title: 'SOP测试白名单', description: '允许 SOP 演示发送', color: '#7C3AED', show_on_sidebar: false },
];

const conversationSeed = [
  { id: 1066, name: '陈小姐', last_message: '我们两个人，预算大约三万元，有适合的路线吗？', ai_state: 'AI_ACTIVE', ai_reason: 'conversation_label_ai', labels: ['ai', '方案介绍'], can_reply: true, status: 'open', ai_mode: 'enabled', ai_mode_source: 'conversation', ai_label_present: true, ai_sync_status: 'synced', version: 8, updated_at: isoAgo(3), email: 'c***@example.com', phone: '+886 9** *** 231', assignee_id: null },
  { id: 1065, name: '林先生', last_message: '可以安排真人跟我确认儿童费用吗？', ai_state: 'HUMAN_HANDOFF', ai_reason: 'handoff_label', labels: ['人工接管', '报价中'], can_reply: true, status: 'open', ai_mode: 'disabled', ai_mode_source: 'handoff', ai_label_present: false, ai_sync_status: 'synced', version: 5, updated_at: isoAgo(8), email: 'l***@mail.com', phone: '', assignee_id: 11 },
  { id: 1064, name: 'May Wong', last_message: '我的 WeChat 是 maytravel88，请把方案发给我。', ai_state: 'HUMAN_HANDOFF', ai_reason: 'lead_captured', labels: ['已留资', '人工接管'], can_reply: true, status: 'open', ai_mode: 'disabled', ai_mode_source: 'handoff', ai_label_present: false, ai_sync_status: 'synced', version: 11, updated_at: isoAgo(15), email: 'm***@example.com', phone: '', assignee_id: 12 },
  { id: 1063, name: 'Jonathan Yang', last_message: '四月上旬还有东京赏樱团吗？', ai_state: 'AI_ACTIVE', ai_reason: 'inbox_enabled', labels: ['ai'], can_reply: true, status: 'open', ai_mode: 'inherit', ai_mode_source: 'inbox', ai_label_present: true, ai_sync_status: 'synced', version: 4, updated_at: isoAgo(31), email: '', phone: '', assignee_id: null },
  { id: 1062, name: 'Pauline Wang', last_message: '收到，我先和家人讨论一下，谢谢。', ai_state: 'AI_PAUSED_CONVERSATION', ai_reason: 'conversation_disabled', labels: ['方案介绍'], can_reply: true, status: 'open', ai_mode: 'disabled', ai_mode_source: 'conversation', ai_label_present: false, ai_sync_status: 'synced', version: 6, updated_at: isoAgo(52), email: '', phone: '', assignee_id: null },
  { id: 1061, name: 'Yokoolay Huang', last_message: '之前提供的行程可以再调整一天吗？', ai_state: 'AI_ACTIVE', ai_reason: 'inbox_enabled', labels: ['ai', '报价中'], can_reply: true, status: 'open', ai_mode: 'inherit', ai_mode_source: 'inbox', ai_label_present: true, ai_sync_status: 'synced', version: 9, updated_at: isoAgo(76), email: 'y***@example.com', phone: '', assignee_id: null },
  { id: 1060, name: 'Antonio Lee', last_message: '我想投诉，实际说明和广告内容不一致。', ai_state: 'HUMAN_HANDOFF', ai_reason: 'complaint_label', labels: ['客诉', '人工接管'], can_reply: true, status: 'open', ai_mode: 'disabled', ai_mode_source: 'handoff', ai_label_present: false, ai_sync_status: 'synced', version: 7, updated_at: isoAgo(110), email: '', phone: '+852 6*** 112', assignee_id: 12 },
  { id: 1059, name: 'Sandy Chen', last_message: '请问桃花节行程包含哪些景点？', ai_state: 'CHANNEL_BLOCKED', ai_reason: 'can_reply_false', labels: ['方案介绍'], can_reply: false, status: 'resolved', ai_mode: 'inherit', ai_mode_source: 'inbox', ai_label_present: false, ai_sync_status: 'synced', version: 3, updated_at: isoAgo(240), email: '', phone: '', assignee_id: null },
].map((item) => ({ ...item, channel: 'Channel::FacebookPage', inbox: 'CITS 国际旅游 - China2Go', inbox_id: 128859, chatwoot_url: '#demo' }));

let conversations = clone(conversationSeed);

const messagesByConversation: Record<number, JsonRecord[]> = {
  1066: [
    { id: 1, direction: 'incoming', private: false, content_type: 'text', content: '你好，想了解四月去日本的赏樱行程。', status: 'sent', attribution: 'customer', created_at: isoAgo(18) },
    { id: 2, direction: 'outgoing', private: false, content_type: 'text', content: '可以。请问预计几位出行，以及大致预算范围？', status: 'delivered', attribution: 'ai', created_at: isoAgo(17) },
    { id: 3, direction: 'activity', private: false, content_type: 'text', content: '王顾问 添加 ai', status: 'sent', attribution: 'system', created_at: isoAgo(16) },
    { id: 4, direction: 'incoming', private: false, content_type: 'text', content: '我们两个人，预算大约三万元，有适合的路线吗？', status: 'sent', attribution: 'customer', created_at: isoAgo(3) },
    { id: 5, direction: 'outgoing', private: false, content_type: 'text', content: '有的，我可以先按东京、富士山与河口湖方向整理两种方案，并分别标注住宿和交通差异。', status: 'delivered', attribution: 'ai', created_at: isoAgo(2) },
  ],
  1065: [
    { id: 11, direction: 'incoming', private: false, content_type: 'text', content: '两个大人带一个小朋友，儿童费用怎么算？', status: 'sent', attribution: 'customer', created_at: isoAgo(22) },
    { id: 12, direction: 'outgoing', private: false, content_type: 'text', content: '儿童价格会根据年龄、是否占床和机票规则确认。', status: 'delivered', attribution: 'ai', created_at: isoAgo(21) },
    { id: 13, direction: 'incoming', private: false, content_type: 'text', content: '可以安排真人跟我确认儿童费用吗？', status: 'sent', attribution: 'customer', created_at: isoAgo(8) },
    { id: 14, direction: 'activity', private: false, content_type: 'text', content: '系统 添加 人工接管', status: 'sent', attribution: 'system', created_at: isoAgo(8) },
    { id: 15, direction: 'activity', private: false, content_type: 'text', content: 'Assigned to 王顾问 by Handoff Policy', status: 'sent', attribution: 'system', created_at: isoAgo(7) },
  ],
  1064: [
    { id: 21, direction: 'incoming', private: false, content_type: 'text', content: '我的 WeChat 是 maytravel88，请把方案发给我。', status: 'sent', attribution: 'customer', created_at: isoAgo(15) },
    { id: 22, direction: 'activity', private: false, content_type: 'text', content: '系统 添加 已留资', status: 'sent', attribution: 'system', created_at: isoAgo(14) },
  ],
  1063: [
    { id: 31, direction: 'incoming', private: false, content_type: 'text', content: '四月上旬还有东京赏樱团吗？', status: 'sent', attribution: 'customer', created_at: isoAgo(31) },
    { id: 32, direction: 'outgoing', private: false, content_type: 'text', content: '四月上旬有两条路线可选。我先确认您的出发城市和人数，再给您匹配方案。', status: 'delivered', attribution: 'ai', created_at: isoAgo(30) },
  ],
  1062: [{ id: 41, direction: 'incoming', private: false, content_type: 'text', content: '收到，我先和家人讨论一下，谢谢。', status: 'sent', attribution: 'customer', created_at: isoAgo(52) }],
  1061: [{ id: 51, direction: 'incoming', private: false, content_type: 'text', content: '之前提供的行程可以再调整一天吗？', status: 'sent', attribution: 'customer', created_at: isoAgo(76) }],
  1060: [{ id: 61, direction: 'incoming', private: false, content_type: 'text', content: '我想投诉，实际说明和广告内容不一致。', status: 'sent', attribution: 'customer', created_at: isoAgo(110) }],
  1059: [{ id: 71, direction: 'incoming', private: false, content_type: 'text', content: '请问桃花节行程包含哪些景点？', status: 'sent', attribution: 'customer', created_at: isoAgo(240) }],
};

let handoffs = [
  { id: 201, conversation_id: 1065, customer_name: '林先生', inbox: 'CITS 国际旅游 - China2Go', reason_code: 'customer_requested_human', reason_detail: '客户要求真人确认儿童费用', priority: 'P1', status: 'pending', sla_due_at: isoAfter(18), created_at: isoAgo(12), version: 1, labels: ['人工接管', '报价中'] },
  { id: 202, conversation_id: 1060, customer_name: 'Antonio Lee', inbox: 'CITS 国际旅游 - China2Go', reason_code: 'complaint', reason_detail: '客户反馈宣传内容与实际说明不一致', priority: 'P1', status: 'claimed', assignee: '陈主管', assignee_user_id: 2, sla_due_at: isoAgo(55), created_at: isoAgo(85), version: 2, labels: ['客诉', '人工接管'] },
  { id: 203, conversation_id: 1064, customer_name: 'May Wong', inbox: 'CITS 国际旅游 - China2Go', reason_code: 'lead_captured', reason_detail: '已取得 WeChat，转销售继续跟进', priority: 'P2', status: 'pending', sla_due_at: isoAfter(23), created_at: isoAgo(7), version: 1, labels: ['已留资', '人工接管'] },
  { id: 204, conversation_id: 1058, customer_name: '何先生', inbox: 'CITS 国际旅游 - China2Go', reason_code: 'ai_error_threshold', reason_detail: '企业 AI 接口连续超时，已执行安全降级', priority: 'P2', status: 'completed', assignee: '王顾问', assignee_user_id: 1, sla_due_at: isoAgo(210), created_at: isoAgo(260), version: 3, labels: ['人工接管'] },
];

interface DemoPlatformUser {
  id: number;
  email: string;
  display_name: string;
  role: string;
  active: boolean;
  chatwoot_agent_id?: number;
  inbox_binding_ids: number[];
  temporary_password?: string;
}

let platformUsers: DemoPlatformUser[] = [
  { id: 1, email: 'admin@example.test', display_name: '演示管理员', role: 'admin', active: true, chatwoot_agent_id: 11, inbox_binding_ids: [1] },
  { id: 2, email: 'supervisor@example.test', display_name: '陈主管', role: 'supervisor', active: true, chatwoot_agent_id: 12, inbox_binding_ids: [1] },
  { id: 3, email: 'agent@example.test', display_name: '林顾问', role: 'agent', active: true, chatwoot_agent_id: 13, inbox_binding_ids: [1] },
];

const inboxes = [
  { id: 1, chatwoot_inbox_id: 128859, name: 'CITS 国际旅游 - China2Go', channel_type: 'Channel::FacebookPage', ai_enabled: true, status: 'active', last_synced_at: isoAgo(2) },
  { id: 2, chatwoot_inbox_id: 128853, name: '官网在线咨询', channel_type: 'Channel::WebWidget', ai_enabled: false, status: 'active', last_synced_at: isoAgo(5) },
];

function requestBody(init: RequestInit): JsonRecord {
  if (!init.body || typeof init.body !== 'string') return {};
  try { return JSON.parse(init.body) as JsonRecord; } catch { return {}; }
}

function conversationSummary(item: typeof conversations[number]) {
  const { email: _email, phone: _phone, assignee_id: _assignee, ...summary } = item;
  return summary;
}

function findConversation(path: string) {
  const id = Number(path.match(/^\/conversations\/(\d+)/)?.[1]);
  return conversations.find((item) => item.id === id);
}

function controlsFor(item: typeof conversations[number]) {
  return {
    assignee: agents.find((agent) => agent.id === item.assignee_id) ?? null,
    team: item.assignee_id ? { id: 1, name: '旅游顾问组' } : null,
    agents,
    labels: labelDefinitions,
    conversation_labels: item.labels,
    ai_mode: item.ai_mode,
    ai_label_present: item.ai_label_present,
    ai_sync_status: item.ai_sync_status,
    can_create_labels: true,
  };
}

async function pause() {
  await new Promise((resolve) => window.setTimeout(resolve, 90));
}

export async function demoApi<T>(path: string, init: RequestInit = {}): Promise<T> {
  await pause();
  const method = (init.method ?? 'GET').toUpperCase();
  const url = new URL(path, 'https://demo.invalid');
  const pathname = url.pathname;
  const body = requestBody(init);

  if (pathname === '/auth/me') return clone({ id: 1, email: 'admin@example.test', display_name: '演示管理员', roles: ['admin'], permissions: ['*'], must_change_password: false }) as T;
  if (pathname === '/auth/csrf') return { csrf_token: 'demo-csrf-token' } as T;
  if (pathname === '/auth/login') return clone({ user: { id: 1, email: 'admin@example.test', display_name: '演示管理员', roles: ['admin'], permissions: ['*'], must_change_password: false }, csrf_token: 'demo-csrf-token' }) as T;
  if (pathname === '/auth/logout') return undefined as T;
  if (pathname === '/notifications') return clone({ items: [{ id: 1, title: '人工接管待处理', body: '林先生正在等待真人确认儿童费用', conversation_id: 1065 }, { id: 2, title: 'SOP 演练完成', body: '桃花节白名单验证已生成 12 条演练记录', read_at: isoAgo(20) }], unread: 1 }) as T;
  if (/^\/notifications\/\d+\/read$/.test(pathname)) return {} as T;

  if (pathname === '/conversations/filters') return clone({ inboxes: inboxes.map((item) => ({ id: item.chatwoot_inbox_id, name: item.name, channel: item.channel_type })), labels: labelDefinitions.map(({ title, color }) => ({ title, color })), ai_states: ['AI_ACTIVE', 'HUMAN_HANDOFF', 'AI_PAUSED_CONVERSATION', 'CHANNEL_BLOCKED'] }) as T;
  if (pathname === '/conversations' && method === 'GET') {
    const query = (url.searchParams.get('q') ?? '').trim().toLowerCase();
    const inboxId = url.searchParams.get('inbox_id');
    const label = url.searchParams.get('label');
    const aiState = url.searchParams.get('ai_state');
    const page = Math.max(1, Number(url.searchParams.get('page') ?? 1));
    const pageSize = Math.max(1, Number(url.searchParams.get('page_size') ?? 25));
    let items = conversations.filter((item) => !query || `${item.name} ${item.id} ${item.last_message}`.toLowerCase().includes(query));
    if (inboxId) items = items.filter((item) => String(item.inbox_id) === inboxId);
    if (label) items = items.filter((item) => item.labels.includes(label));
    if (aiState) items = items.filter((item) => item.ai_state === aiState);
    return clone({ items: items.slice((page - 1) * pageSize, page * pageSize).map(conversationSummary), total: items.length, page, page_size: pageSize }) as T;
  }
  if (/^\/conversations\/\d+\/messages$/.test(pathname)) {
    const item = findConversation(pathname);
    return clone({ items: item ? messagesByConversation[item.id] ?? [] : [] }) as T;
  }
  if (/^\/conversations\/\d+\/controls$/.test(pathname)) {
    const item = findConversation(pathname);
    return clone(item ? controlsFor(item) : {}) as T;
  }
  if (/^\/conversations\/\d+\/assignment$/.test(pathname) && method === 'PUT') {
    const item = findConversation(pathname);
    if (item) {
      item.assignee_id = body.assignee_id ? Number(body.assignee_id) : null;
      if (body.pause_ai && item.assignee_id) { item.ai_mode = 'disabled'; item.ai_state = 'HUMAN_HANDOFF'; item.ai_reason = 'manual_assignment'; item.labels = [...new Set([...item.labels.filter((label) => label !== 'ai'), '人工接管'])]; item.ai_label_present = false; }
      item.version += 1;
    }
    return clone(item ? controlsFor(item) : {}) as T;
  }
  if (/^\/conversations\/\d+\/ai-mode$/.test(pathname) && method === 'PUT') {
    const item = findConversation(pathname);
    const enabled = Boolean(body.enabled);
    if (item) { item.ai_mode = enabled ? 'enabled' : 'disabled'; item.ai_state = enabled ? 'AI_ACTIVE' : 'AI_PAUSED_CONVERSATION'; item.ai_reason = enabled ? 'conversation_enabled' : 'conversation_disabled'; item.ai_label_present = enabled; item.labels = enabled ? [...new Set([...item.labels.filter((label) => label !== '人工接管'), 'ai'])] : item.labels.filter((label) => label !== 'ai'); item.version += 1; }
    return clone(item ?? {}) as T;
  }
  if (/^\/conversations\/\d+\/labels$/.test(pathname) && method === 'PUT') {
    const item = findConversation(pathname);
    if (item && Array.isArray(body.labels)) { item.labels = body.labels as string[]; item.ai_label_present = item.labels.includes('ai'); item.version += 1; }
    return clone(item ?? {}) as T;
  }
  if (/^\/conversations\/\d+$/.test(pathname)) {
    const item = findConversation(pathname);
    return clone(item ? { ...conversationSummary(item), contact: { name: item.name, email: item.email || undefined, phone_number: item.phone || undefined, pii_masked: !item.assignee_id } } : {}) as T;
  }
  if (pathname === '/labels' && method === 'POST') {
    const created = { id: labelDefinitions.length + 1, title: String(body.title), description: String(body.description ?? ''), color: String(body.color ?? '#64748B'), show_on_sidebar: true };
    labelDefinitions.push(created);
    return clone(created) as T;
  }

  if (pathname === '/handoffs' && method === 'GET') {
    const status = url.searchParams.get('status') ?? 'pending';
    const query = (url.searchParams.get('q') ?? '').toLowerCase();
    const page = Math.max(1, Number(url.searchParams.get('page') ?? 1));
    const pageSize = Math.max(1, Number(url.searchParams.get('page_size') ?? 10));
    const filtered = handoffs.filter((item) => item.status === status && (!query || `${item.customer_name} ${item.conversation_id} ${item.reason_detail}`.toLowerCase().includes(query)));
    const counts = { pending: handoffs.filter((item) => item.status === 'pending').length, claimed: handoffs.filter((item) => item.status === 'claimed').length, completed: handoffs.filter((item) => item.status === 'completed').length };
    return clone({ items: filtered.slice((page - 1) * pageSize, page * pageSize), counts, total: filtered.length, page }) as T;
  }
  if (pathname === '/handoffs' && method === 'POST') {
    const conversation = conversations.find((item) => item.id === Number(body.conversation_id));
    const created = { id: Math.max(...handoffs.map((item) => item.id)) + 1, conversation_id: Number(body.conversation_id), customer_name: conversation?.name ?? '演示客户', inbox: conversation?.inbox ?? inboxes[0].name, reason_code: String(body.reason_code ?? 'manual'), reason_detail: String(body.reason_detail ?? ''), priority: String(body.priority ?? 'P2'), status: 'pending', sla_due_at: isoAfter(30), created_at: new Date().toISOString(), version: 1, labels: ['人工接管'] };
    handoffs.unshift(created);
    if (conversation) { conversation.ai_state = 'HUMAN_HANDOFF'; conversation.ai_mode = 'disabled'; conversation.labels = [...new Set([...conversation.labels.filter((label) => label !== 'ai'), '人工接管'])]; }
    return clone(created) as T;
  }
  const handoffAction = pathname.match(/^\/handoffs\/(\d+)\/(claim|assign|complete|restore-ai)$/);
  if (handoffAction) {
    const item = handoffs.find((task) => task.id === Number(handoffAction[1]));
    if (!item) return {} as T;
    const action = handoffAction[2];
    if (action === 'claim') { item.status = 'claimed'; item.assignee = '演示管理员'; item.assignee_user_id = 1; }
    if (action === 'assign') { const user = platformUsers.find((candidate) => candidate.id === Number(body.user_id)); item.status = 'claimed'; item.assignee = user?.display_name ?? '演示客服'; item.assignee_user_id = user?.id; }
    if (action === 'complete') item.status = 'completed';
    if (action === 'restore-ai') { item.status = 'completed'; const conversation = conversations.find((candidate) => candidate.id === item.conversation_id); if (conversation) { conversation.ai_state = 'AI_ACTIVE'; conversation.ai_mode = 'enabled'; conversation.labels = [...new Set([...conversation.labels.filter((label) => label !== '人工接管'), 'ai'])]; } }
    item.version += 1;
    return clone(item) as T;
  }

  if (pathname === '/media' && method === 'POST') return { id: 9001 } as T;

  if (pathname === '/bi/overview') return clone({ conversations: 186, incoming_messages: 742, outgoing_messages: 619, ai_only: 104, mixed: 47, human_only: 35, ai_handled: 151, handoffs: 38, handoff_pending: 2, handoff_overdue: 1, leads: 45, conversions: 18, ai_errors: 3, inferred_history: 22 }) as T;
  if (pathname === '/bi/trends') return clone({ items: [{ day: '周一', incoming: 84, ai: 62, human: 21 }, { day: '周二', incoming: 101, ai: 77, human: 24 }, { day: '周三', incoming: 96, ai: 70, human: 28 }, { day: '周四', incoming: 118, ai: 86, human: 31 }, { day: '周五', incoming: 127, ai: 93, human: 35 }, { day: '周六', incoming: 109, ai: 79, human: 30 }, { day: '周日', incoming: 107, ai: 82, human: 29 }] }) as T;
  if (pathname === '/bi/funnel') return clone({ items: [{ name: '有效咨询', value: 186 }, { name: '方案介绍', value: 134 }, { name: '进入报价', value: 85 }, { name: '成功留资', value: 45 }, { name: '确认成交', value: 18 }] }) as T;

  if (pathname === '/settings/chatwoot') return clone({ configured: true, base_url: 'https://app.chatwoot.com', account_id: 100001, token_last4: 'DEMO', status: 'connected', last_tested_at: isoAgo(4), webhook_url: 'https://demo.invalid/webhook' }) as T;
  if (pathname === '/settings/inboxes') return clone(inboxes) as T;
  if (pathname === '/settings/chatwoot/test') return { inbox_count: inboxes.length } as T;
  if (pathname === '/settings/chatwoot/sync') return { inboxes: inboxes.length } as T;
  if (pathname === '/settings/chatwoot/webhook') return clone({ webhook_id: 901, url: 'https://demo.invalid/v1/webhooks/chatwoot/DEMO', events: ['message_created', 'message_updated', 'conversation_created', 'conversation_updated', 'conversation_status_changed', 'contact_created', 'contact_updated'], last_received_at: isoAgo(2) }) as T;
  if (pathname === '/settings/ai') return clone({ trigger_text: '测试人员触发消息', reply_text: '测试人员回复消息' }) as T;
  if (pathname === '/settings/history-sync/status') return clone({ status: 'completed', phase: 'complete', current_page: 4, total_items: 66, completed_items: 66, failed_items: 0, updated_at: isoAgo(35) }) as T;
  if (pathname === '/settings/label-mappings') return clone({ handoff_labels: ['人工接管', '客诉'], contact_block_labels: ['拒绝联系', '黑名单'], lead_labels: ['已留资'], conversion_labels: ['已成交'], stage_labels: ['新咨询', '方案介绍', '报价中'], sop_whitelist_label: 'SOP测试白名单' }) as T;
  if (pathname === '/settings/resources') return clone({ agents }) as T;
  if (pathname === '/settings/notifications') return clone({ enabled: true, url: 'https://notify.example.test/handoff', event_types: ['handoff.created', 'handoff.overdue'], secret_configured: true }) as T;
  if (pathname === '/audit-logs') return clone({ items: [{ id: 1, action: 'conversation.ai_mode.updated', resource_type: 'conversation', resource_id: '1066', user_id: 1, created_at: isoAgo(12) }, { id: 2, action: 'handoff.claimed', resource_type: 'handoff', resource_id: '202', user_id: 2, created_at: isoAgo(55) }, { id: 3, action: 'sop.published', resource_type: 'sop', resource_id: '301', user_id: 1, created_at: isoAgo(1440) }] }) as T;
  if (pathname === '/users' && method === 'GET') return clone({ items: platformUsers }) as T;
  if (pathname === '/users' && method === 'POST') {
    const created = { id: platformUsers.length + 1, email: String(body.email), display_name: String(body.display_name), role: String(body.role ?? 'agent'), active: true, chatwoot_agent_id: body.chatwoot_agent_id ? Number(body.chatwoot_agent_id) : undefined, inbox_binding_ids: (body.inbox_binding_ids as number[]) ?? [], temporary_password: 'Demo-Only-2026' };
    platformUsers.push(created);
    return clone(created) as T;
  }
  const userPatch = pathname.match(/^\/users\/(\d+)$/);
  if (userPatch && method === 'PATCH') { const index = platformUsers.findIndex((item) => item.id === Number(userPatch[1])); if (index >= 0) platformUsers[index] = { ...platformUsers[index], ...body }; return clone(platformUsers[index] ?? {}) as T; }

  if (method !== 'GET') return clone({ demo: true, saved: true }) as T;
  return clone({}) as T;
}
