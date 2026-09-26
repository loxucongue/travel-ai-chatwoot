export type PageId = 'overview' | 'playground' | 'conversations' | 'handoff' | 'sops' | 'settings';
export type AiState = 'ai_active' | 'human_handoff' | 'ai_paused' | 'completed';
export type JourneyStage = '新咨询' | '方案介绍' | '报价中' | '已留资' | '客诉';
export type Priority = 'P1' | 'P2' | 'P3';

export interface Conversation {
  id: number;
  contactId: number;
  name: string;
  initials: string;
  channel: 'Facebook' | 'Instagram';
  inbox: string;
  lastMessage: string;
  time: string;
  aiState: AiState;
  stage: JourneyStage;
  labels: string[];
  canReply: boolean;
  team: string;
  assignee: string;
  email?: string;
  phone?: string;
  wechat?: string;
  messages: Array<{
    id: number;
    direction: 'incoming' | 'outgoing' | 'system';
    sender: string;
    content: string;
    time: string;
    source?: 'AI' | '人工';
  }>;
}

export interface HandoffItem {
  id: number;
  conversationId: number;
  name: string;
  initials: string;
  channel: string;
  reason: string;
  reasonType: '客户要求人工' | '客诉' | '已留资' | 'AI 异常';
  priority: Priority;
  waitSeconds: number;
  stage: JourneyStage;
  team: string;
  assignee: string;
  status: 'pending' | 'processing' | 'completed';
}

export interface SopItem {
  id: number;
  name: string;
  description: string;
  status: 'running' | 'paused' | 'draft';
  audience: string;
  channels: string[];
  steps: number;
  enrolled: number;
  delivered: number;
  replyRate: number;
  leadRate: number;
  updatedAt: string;
}

export const conversations: Conversation[] = [
  {
    id: 1028,
    contactId: 1012447123,
    name: '林小姐',
    initials: '林',
    channel: 'Facebook',
    inbox: 'CITS 國旅環球 - China2Go',
    lastMessage: '预算可以再调整吗？我想找真人确认。',
    time: '2 分钟前',
    aiState: 'human_handoff',
    stage: '报价中',
    labels: ['人工接管', '报价中'],
    canReply: true,
    team: '销售一组',
    assignee: '待领取',
    messages: [
      { id: 1, direction: 'incoming', sender: '林小姐', content: '你好，想咨询你们四月去日本的行程。', time: '10:21' },
      { id: 2, direction: 'outgoing', sender: 'AI 助手', content: '可以。请问您预计几位出行，以及更偏好关西还是东京周边？', time: '10:21', source: 'AI' },
      { id: 3, direction: 'incoming', sender: '林小姐', content: '两位，东京周边。预算可以再调整吗？我想找真人确认。', time: '10:24' },
      { id: 4, direction: 'system', sender: '系统', content: '因“客户要求人工”暂停 AI，并分配至销售一组。', time: '10:24' },
    ],
  },
  {
    id: 1027,
    contactId: 1012141622,
    name: '陳先生',
    initials: '陳',
    channel: 'Facebook',
    inbox: 'CITS 國旅環球 - China2Go',
    lastMessage: '请问四月份还有日本赏樱团吗？',
    time: '5 分钟前',
    aiState: 'ai_active',
    stage: '方案介绍',
    labels: ['AI处理中'],
    canReply: true,
    team: '未分配',
    assignee: 'AI 助手',
    email: 'chen***@gmail.com',
    messages: [
      { id: 1, direction: 'incoming', sender: '陳先生', content: '请问四月份还有日本赏樱团吗？', time: '10:18' },
      { id: 2, direction: 'outgoing', sender: 'AI 助手', content: '目前四月上旬仍有东京及富士山赏樱行程。我先确认您的出发城市和人数。', time: '10:18', source: 'AI' },
    ],
  },
  {
    id: 1026,
    contactId: 1012142568,
    name: 'May Wong',
    initials: 'MW',
    channel: 'Facebook',
    inbox: 'CITS 國旅環球 - China2Go',
    lastMessage: '我的 WeChat 是 maytravel88。',
    time: '11 分钟前',
    aiState: 'human_handoff',
    stage: '已留资',
    labels: ['已留资', '人工接管'],
    canReply: true,
    team: '销售二组',
    assignee: '待领取',
    wechat: 'mayt****88',
    messages: [
      { id: 1, direction: 'incoming', sender: 'May Wong', content: '我的 WeChat 是 maytravel88，可以把报价发给我。', time: '10:13' },
      { id: 2, direction: 'system', sender: '系统', content: '检测到待确认联系方式。确认后标记为“已留资”。', time: '10:13' },
    ],
  },
  {
    id: 1025,
    contactId: 1012167252,
    name: 'Antonio Lee',
    initials: 'AL',
    channel: 'Facebook',
    inbox: 'CITS 國旅環球 - China2Go',
    lastMessage: '下午好，我想了解这个行程的价格。',
    time: '28 分钟前',
    aiState: 'ai_active',
    stage: '报价中',
    labels: ['报价中'],
    canReply: true,
    team: '未分配',
    assignee: 'AI 助手',
    phone: '+886 9** *** 218',
    messages: [
      { id: 1, direction: 'incoming', sender: 'Antonio Lee', content: '下午好，我想了解这个行程的价格。', time: '09:56' },
      { id: 2, direction: 'outgoing', sender: 'AI 助手', content: '可以，请问您希望了解哪一个目的地和出发日期？', time: '09:56', source: 'AI' },
    ],
  },
];

export const handoffs: HandoffItem[] = [
  { id: 1, conversationId: 1028, name: '林小姐', initials: '林', channel: 'Facebook', reason: '客户明确要求人工，并询问预算调整', reasonType: '客户要求人工', priority: 'P1', waitSeconds: 732, stage: '报价中', team: '销售一组', assignee: '待领取', status: 'pending' },
  { id: 2, conversationId: 1019, name: '周先生', initials: '周', channel: 'Facebook', reason: '投诉行程与已确认内容不一致', reasonType: '客诉', priority: 'P1', waitSeconds: 421, stage: '客诉', team: '客服主管', assignee: '李主管', status: 'processing' },
  { id: 3, conversationId: 1026, name: 'May Wong', initials: 'MW', channel: 'Facebook', reason: '已确认 WeChat，转销售继续跟进', reasonType: '已留资', priority: 'P2', waitSeconds: 248, stage: '已留资', team: '销售二组', assignee: '待领取', status: 'pending' },
  { id: 4, conversationId: 1017, name: '何先生', initials: '何', channel: 'Facebook', reason: 'AI 接口连续超时，已执行安全降级', reasonType: 'AI 异常', priority: 'P2', waitSeconds: 188, stage: '方案介绍', team: '销售一组', assignee: '待领取', status: 'pending' },
  { id: 5, conversationId: 1013, name: 'Sandy Chen', initials: 'SC', channel: 'Facebook', reason: '需要人工确认多人团报价', reasonType: '客户要求人工', priority: 'P3', waitSeconds: 94, stage: '报价中', team: '销售一组', assignee: '王俐斐', status: 'processing' },
];

export const initialSops: SopItem[] = [
  { id: 1, name: '方案发送后跟进', description: '方案发送后 24 小时仍未回复时提醒一次', status: 'running', audience: '方案介绍 · 未回复 · 非人工', channels: ['Facebook'], steps: 3, enrolled: 328, delivered: 295, replyRate: 18.6, leadRate: 7.4, updatedAt: '今天 09:18' },
  { id: 2, name: '报价后 24 小时提醒', description: '报价中且 24 小时无新入站消息', status: 'running', audience: '报价中 · 未留资 · 可回复', channels: ['Facebook'], steps: 2, enrolled: 96, delivered: 88, replyRate: 27.1, leadRate: 12.5, updatedAt: '昨天 16:42' },
  { id: 3, name: '节庆行程召回', description: '针对已授权活动标签客户的合规召回', status: 'draft', audience: '活动标签 · 非拒绝联系', channels: ['Facebook', 'Instagram'], steps: 3, enrolled: 0, delivered: 0, replyRate: 0, leadRate: 0, updatedAt: '8 月 14 日' },
];

export const overviewSeries = [
  { day: '周一', ai: 118, human: 42 },
  { day: '周二', ai: 136, human: 48 },
  { day: '周三', ai: 152, human: 46 },
  { day: '周四', ai: 166, human: 55 },
  { day: '周五', ai: 149, human: 51 },
  { day: '周六', ai: 188, human: 67 },
  { day: '周日', ai: 174, human: 58 },
];

export const funnelData = [
  { name: '有效咨询', value: 186, percent: 100 },
  { name: '方案介绍', value: 134, percent: 72 },
  { name: '进入报价', value: 85, percent: 46 },
  { name: '成功留资', value: 45, percent: 24 },
  { name: '人工跟进', value: 34, percent: 18 },
];

export function aiStateLabel(state: AiState) {
  return {
    ai_active: 'AI 接管',
    human_handoff: '人工接管',
    ai_paused: 'AI 已暂停',
    completed: '已完成',
  }[state];
}

export function formatWait(seconds: number) {
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return minutes > 0 ? `${minutes}分 ${rest}秒` : `${rest}秒`;
}
