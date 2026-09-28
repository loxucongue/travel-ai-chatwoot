import { FormEvent, useEffect, useMemo, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  Bot,
  CalendarClock,
  CheckCircle2,
  ChevronLeft,
  FastForward,
  Image as ImageIcon,
  Menu,
  PanelRight,
  Plus,
  Send,
  ShieldCheck,
  Sparkles,
  Trash2,
  UserRound,
  Workflow,
  X,
} from 'lucide-react';
import { useSearchParams } from 'react-router-dom';
import { api, API_BASE, ApiError, DEMO_MODE } from '../api';
import { Badge, EmptyState } from '../components';
import { sopReason } from './sop-reasons';

import { DeliveryDetails, DeliveryStatus, RuntimeDetails } from '../DeliveryDetails';
import { customerTranscriptMessage, deliveryStates, type RuntimeEvidence } from '../delivery';

type RouteVariant = string;
type OptionItem = { title?: string; value?: string };

type Presentation = {
  type?: string;
  route_variant?: string;
  route_ids?: string[];
  criteria?: string[];
  recommendation?: string;
  material_keys?: string[];
  evidence_refs?: string[];
  topics?: string[];
};

type SessionMessage = {
  id: string | number;
  direction: 'incoming' | 'outgoing';
  content: string;
  content_type?: string;
  content_attributes?: { items?: OptionItem[] } | Record<string, unknown>;
  created_at: string;
  timeline_sequence?: number;
  status?: string;
  private?: boolean;
  reason?: string;
  delivery_item?: Record<string, unknown>;
  _delivery_item?: Record<string, unknown>;
  media_id?: number;
  source?: 'ai' | 'sop' | string;
  run_id?: number;
};

type TimelineEvent = {
  id: string;
  event_type: string;
  content: string;
  created_at: string;
  timeline_sequence?: number;
};

type RunRecord = {
  id: number;
  module: 'reply' | 'silence_touch' | string;
  status: string;
  error_code?: string;
  decision: {
    safety_flags?: string[];
    action?: string;
    reply?: string;
    route_variant?: string;
    lead_action?: string;
    handoff_reason?: string;
    content_group_key?: string;
    covered_content_groups?: string[];
    journey_stage?: string;
    touch_goal?: string;
    touch_reason?: string;
    profile_updates?: Record<string, { value?: unknown; confidence?: number; evidence_quote?: string; reason?: string }>;
    presentations?: Presentation[];
  };
  trace: RuntimeEvidence['trace'] & { total_ms?: number };
};

type JourneySession = {
  id: number;
  engine_version: 'v1' | 'v2' | 'v3';
  engine_release_id: string;
  generation: number;
  environment: 'playground';
  mode: 'journey';
  virtual_now: string;
  pending: boolean;
  outbound: false;
  controls: {
    route_variant?: RouteVariant;
    human: boolean;
    can_reply: boolean;
    ai_enabled: boolean;
    timeline_events?: TimelineEvent[];
    lead_capture?: { status?: string };
    journey?: { stage?: string; slots?: Record<string, unknown>; sent_content_groups?: string[] };
  };
  simulation: {
    status: 'running' | 'paused' | 'completed' | 'stopped';
    speed_multiplier: number;
    sop_name?: string;
  };
  memory: Record<string, { value: unknown; quote: string }>;
  reception_state?: {
    journey_stage?: string;
    customer_profile?: Record<string, { value: unknown; quote?: string; confidence?: number; reason?: string; kind?: string }>;
    last_touch?: RunRecord['decision'] | null;
    next_touch_at?: string | null;
    next_touch_status?: string | null;
    last_warning?: string | null;
    can_retry?: boolean;
  };
  messages: SessionMessage[];
  runs: RunRecord[];
  jobs: {
    id: number;
    node_key: string;
    scheduled_at?: string;
    status: string;
    reason?: string;
    payload?: { model_decision?: RunRecord['decision'] };
  }[];
};

type SessionSummary = {
  id: number;
  mode: string;
  created_at: string;
  route_variant?: RouteVariant;
  status?: string;
  engine_version?: 'v1' | 'v2' | 'v3';
};

const ROUTE_NAMES: Record<RouteVariant, string> = {
  peach_9d_2027: '桃花 9 日',
  peach_11d_2027: '桃花＋珠峰 11 日',
};

const QUICK_MESSAGES = [
  '我想先看看有哪些行程',
  '我们2位，明年3月底出发，住宿和价格怎样？',
  '那11日加珠峰的区别是什么？',
  '我们12个人想包团',
  '可以用LINE联系吗？',
  '我的LINE是 abc123',
];

const STATUS_LABELS: Record<string, string> = {
  running: '接待中',
  paused: '已暂停',
  completed: '已完成',
  stopped: '已停止',
  pending: '等待处理',
  processing: '处理中',
  failed: '失败',
  draft: '准备发送',
  scheduled: '等待客户沉默',
  waiting_dependency: '等待前序',
  simulated_delivered: '已模拟送达',
  already_provided: '此前已提供',
  skipped: '已跳过',
  cancelled: '客户回复后取消',
  blocked: '已阻断',
  model_pending: 'AI 正在生成必发内容',
  skipped_model_failure: '模型失败，本次已跳过',
  submission_unknown: '发送状态待核对',
};

const EVENT_LABELS: Record<string, string> = {
  journey_started: '新客户进入',
  sop_enrolled: 'AI 已识别线路并进入 SOP',
  sop_exited: '客户回复，旧 SOP 停止',
  ai_replied: 'AI 已回复',
  ai_blocked: 'AI 未回复',
  ai_handoff: '已转人工',
  silence_touch_due: '沉默触达到期',
  silence_touch_sent: '沉默触达已发送',
  silence_model_warning: '模型连续失败预警',
  journey_completed: '演练完成',
};

const MEMORY_LABELS: Record<string, string> = {
  party_size: '同行人数',
  departure_window: '出发时间',
  destination: '咨询项目',
  budget: '预算',
  private_group: '包团偏好',
  peak_preference: '珠峰偏好',
  first_time_tibet: '是否第一次进藏',
  permit_awareness: '证件 / 入藏手续认知',
  concerns: '客户顾虑',
  decision_status: '决策状态',
  intent_level: '意向等级',
  unresolved_question: '最近未解决问题',
  contact_status: '联系方式状态',
};

const JOURNEY_STAGE_LABELS: Record<string, string> = {
  route_selection: '选择线路', needs_discovery: '了解需求', value_building: '建立价值',
  objection_handling: '处理顾虑', contact_ready: '适合留资', contact_requested: '已索取联系',
  considering: '考虑 / 与家人讨论', captured: '已取得联系', handoff: '人工接管',
  discovering_needs: '了解需求', introducing: '建立价值', answering: '建立价值', completed: '已取得联系',
};

const TOUCH_GOAL_LABELS: Record<string, string> = {
  route_choice: '帮助选择线路', collect_need: '补齐一个关键需求', build_value: '提供新价值',
  handle_objection: '回应顾虑', request_contact: '索取联系方式',
  contact_reminder: '轻提醒联系方式', soft_nurture: '低压力培育',
};

const PRESENTATION_LABELS: Record<string, string> = {
  route_comparison: '线路比较',
  route_details: '线路详情',
  itinerary: '行程展示',
  route_materials: '线路资料',
  suggestions: '下一步建议',
};

const CRITERION_LABELS: Record<string, string> = {
  duration: '天数',
  pace: '节奏',
  hotel: '住宿',
  price: '价格',
  highlights: '亮点',
  itinerary: '行程',
  vehicle: '用车',
  oxygen: '供氧',
  departure: '出发时间',
};

const SOP_NODE_LABELS: Record<string, string> = {
  silence_mainline: '第 1 次沉默跟进',
};

function formatTime(value?: string) {
  return value
    ? new Date(value).toLocaleString('zh-CN', {
      timeZone: 'Asia/Shanghai',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    })
    : '等待';
}

function routeName(value?: string) {
  return value ? ROUTE_NAMES[value] ?? value : '等待 AI 识别';
}

function statusLabel(value?: string) {
  return value ? deliveryStates[value]?.label ?? STATUS_LABELS[value] ?? value : '-';
}

function sopNodeLabel(value?: string) {
  if (!value) return '暂无待执行跟进';
  const dynamicWakeup = /^wakeup_(\d+)$/.exec(value);
  if (dynamicWakeup) return `第 ${Number(dynamicWakeup[1]) + 1} 次沉默跟进`;
  return SOP_NODE_LABELS[value] ?? '沉默跟进';
}

function PresentationSummary({ presentations }: { presentations?: Presentation[] }) {
  if (!presentations?.length) return null;
  return <section className="ai-chat-presentation-summary">
    <h3>结构化展示</h3>
    <div className="ai-chat-presentation-list">
      {presentations.map((item, index) => {
        const routes = item.route_ids?.map(routeName).filter(Boolean) ?? [];
        const route = item.route_variant ? routeName(item.route_variant) : '';
        const criteria = item.criteria?.map(value => CRITERION_LABELS[value] ?? value) ?? [];
        return <article key={`${item.type ?? 'presentation'}-${index}`}>
          <header><strong>{PRESENTATION_LABELS[item.type ?? ''] ?? item.type ?? '展示内容'}</strong>{item.recommendation ? <Badge tone="green">推荐 {routeName(item.recommendation)}</Badge> : null}</header>
          {routes.length ? <p>{routes.join(' · ')}</p> : null}
          {route ? <p>{route}{item.topics?.length ? ` · ${item.topics.join('、')}` : ''}</p> : null}
          {criteria.length ? <p className="ai-chat-presentation-tags">{criteria.map(value => <span key={value}>{value}</span>)}</p> : null}
          {item.material_keys?.length ? <small>资料：{item.material_keys.join('、')}</small> : null}
          {item.evidence_refs?.length ? <small>事实：{item.evidence_refs.join('、')}</small> : null}
        </article>;
      })}
    </div>
  </section>;
}

function hasOptionItems(message: SessionMessage): OptionItem[] {
  const attrs = message.content_attributes as { items?: OptionItem[] } | undefined;
  return Array.isArray(attrs?.items) ? attrs.items : [];
}

function Failure({ error }: { error: unknown }) {
  if (!error) return null;
  const apiError = error as ApiError;
  return <div className="automation-error" role="alert">{apiError.message || apiError.code || '请求失败'}</div>;
}

export default function AutomationPlayground() {
  if (DEMO_MODE) {
    return <EmptyState type="permission" title="公开演示环境未开放 AI 演练" description="" />;
  }
  return <ReceptionPlayground />;
}

function ReceptionPlayground() {
  const [params, setParams] = useSearchParams();
  const queryClient = useQueryClient();
  const [sessionId, setSessionId] = useState<number | null>(Number(params.get('session')) || null);
  const [inboxId, setInboxId] = useState<number | null>(null);
  const [draft, setDraft] = useState('');
  const [historyOpen, setHistoryOpen] = useState(false);
  const [entryMessage, setEntryMessage] = useState('你好，我想咨询旅行行程');
  const streamRef = useRef<HTMLDivElement>(null);

  const inboxes = useQuery({
    queryKey: ['inboxes'],
    queryFn: () => api<{ id: number; name: string; chatwoot_inbox_id: number }[]>('/settings/inboxes'),
  });
  const sessionList = useQuery({
    queryKey: ['playground-sessions'],
    queryFn: () => api<{ items: SessionSummary[] }>('/playground/sessions'),
  });
  const session = useQuery({
    queryKey: ['playground-session', sessionId],
    queryFn: () => api<JourneySession>(`/playground/sessions/${sessionId}`),
    enabled: Boolean(sessionId),
    refetchInterval: sessionId ? 900 : false,
  });

  useEffect(() => {
    if (inboxId == null && inboxes.data?.length) {
      const preferred = inboxes.data.find(item => item.chatwoot_inbox_id === 128859) ?? inboxes.data[0];
      setInboxId(preferred.id);
    }
  }, [inboxId, inboxes.data]);

  useEffect(() => {
    if (streamRef.current) streamRef.current.scrollTop = streamRef.current.scrollHeight;
  }, [session.data?.messages.filter(customerTranscriptMessage).length, session.data?.jobs.length]);

  const mutateSession = useMutation({
    mutationFn: ({ path, body }: { path: string; body?: unknown }) => api<JourneySession>(path, {
      method: 'POST',
      body: body == null ? undefined : JSON.stringify(body),
    }),
    onSuccess: data => {
      setSessionId(data.id);
      setParams({ session: String(data.id) });
      queryClient.setQueryData(['playground-session', data.id], data);
      queryClient.invalidateQueries({ queryKey: ['playground-sessions'] });
    },
  });

  const deleteSession = useMutation({
    mutationFn: (id: number) => api(`/playground/sessions/${id}`, { method: 'DELETE' }),
    onSuccess: (_data, deletedId) => {
      queryClient.removeQueries({ queryKey: ['playground-session', deletedId] });
      if (sessionId === deletedId) {
        setSessionId(null);
        setParams({});
        setDraft('');
      }
      queryClient.invalidateQueries({ queryKey: ['playground-sessions'] });
    },
  });

  const createJourney = () => {
    if (!inboxId) return;
    mutateSession.mutate({
      path: '/playground/sessions',
      body: {
        mode: 'journey',
        inbox_binding_id: inboxId,
        duration_minutes: 525600,
        speed_multiplier: 1,
        entry_message: entryMessage,
        engine_version: 'v3',
      },
    });
  };

  const sendCustomerText = (content: string) => {
    if (!sessionId || !content.trim() || session.data?.simulation.status !== 'running') return;
    mutateSession.mutate({
      path: `/playground/sessions/${sessionId}/messages`,
      body: { content: content.trim(), content_type: 'text', client_key: crypto.randomUUID() },
    });
    setDraft('');
  };

  const advanceNextTouch = () => {
    if (!sessionId) return;
    mutateSession.mutate({ path: `/playground/sessions/${sessionId}/advance-next` });
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    sendCustomerText(draft);
  };

  const error = mutateSession.error || deleteSession.error || session.error || inboxes.error;
  const current = session.data;
  const processing = Boolean(current?.pending || current?.runs.some(run => ['pending', 'processing'].includes(run.status)));
  const journeySessions = (sessionList.data?.items ?? []).filter(item => item.mode === 'journey');
  const openSession = (id: number) => {
    setSessionId(id);
    setParams({ session: String(id) });
    setHistoryOpen(false);
  };

  return <div className="ai-chat-app">
    <PlaygroundSidebar
      sessions={journeySessions}
      activeId={sessionId}
      open={historyOpen}
      busy={inboxes.isLoading || mutateSession.isPending || deleteSession.isPending}
      onCreate={() => { setSessionId(null); setParams({}); setHistoryOpen(false); }}
      onOpen={openSession}
      onDelete={id => deleteSession.mutate(id)}
      onClose={() => setHistoryOpen(false)}
    />
    {historyOpen ? <button className="ai-chat-sidebar-scrim" aria-label="关闭会话列表" onClick={() => setHistoryOpen(false)} /> : null}
    <main className="ai-chat-main">
      <Failure error={error} />
      {current?.reception_state?.can_retry ? <div className="automation-error" role="alert">本轮生成失败，客户问题已保留。<button className="text-button" disabled={mutateSession.isPending} onClick={() => mutateSession.mutate({ path: `/playground/sessions/${current.id}/retry` })}>重试本轮</button></div> : null}
      {!current ? <ChatHome
        inboxName={inboxes.data?.find(item => item.id === inboxId)?.name}
        loading={inboxes.isLoading || mutateSession.isPending}
        onCreate={createJourney}
        onOpenHistory={() => setHistoryOpen(true)}
        entryMessage={entryMessage}
        onEntryChange={setEntryMessage}
      /> : <RehearsalRunner
        session={current}
        processing={processing}
        busy={mutateSession.isPending || deleteSession.isPending}
        draft={draft}
        setDraft={setDraft}
        onSubmit={submit}
        onSendText={sendCustomerText}
        onDelete={() => deleteSession.mutate(current.id)}
        onCreate={() => { setSessionId(null); setParams({}); }}
        onOpenHistory={() => setHistoryOpen(true)}
        onAdvanceNext={advanceNextTouch}
        streamRef={streamRef}
      />}
    </main>
  </div>;
}

function PlaygroundSidebar(props: {
  sessions: SessionSummary[];
  activeId: number | null;
  open: boolean;
  busy: boolean;
  onCreate: () => void;
  onOpen: (id: number) => void;
  onDelete: (id: number) => void;
  onClose: () => void;
}) {
  return <aside className={`ai-chat-sidebar ${props.open ? 'is-open' : ''}`}>
    <div className="ai-chat-sidebar-brand">
      <a href="#/overview"><span><Sparkles size={18} /></span><strong>China2Go AI</strong></a>
      <button onClick={props.onClose} aria-label="关闭会话列表"><X size={18} /></button>
    </div>
    <button className="ai-chat-new" disabled={props.busy} onClick={props.onCreate}><Plus size={17} />新客户演练</button>
    <div className="ai-chat-history-title">最近演练</div>
    <div className="ai-chat-history">
      {!props.sessions.length ? <p>还没有演练会话</p> : props.sessions.slice(0, 30).map(item => <div className={props.activeId === item.id ? 'active' : ''} key={item.id}>
        <button onClick={() => props.onOpen(item.id)}><strong>模拟客户 #{item.id}</strong><small>{item.engine_version?.toUpperCase()} · {routeName(item.route_variant)} · {statusLabel(item.status)}</small></button>
        <button title="删除演练" disabled={props.busy} onClick={() => props.onDelete(item.id)}><Trash2 size={14} /></button>
      </div>)}
    </div>
    <div className="ai-chat-sidebar-footer">
      <div><ShieldCheck size={15} /><span><strong>沙盒模式</strong><small>不会联系真实客户</small></span></div>
      <a href="#/overview"><ChevronLeft size={15} />返回运营后台</a>
    </div>
  </aside>;
}

function ChatHome(props: { inboxName?: string; loading: boolean; onCreate: () => void; onOpenHistory: () => void; entryMessage: string; onEntryChange: (value: string) => void }) {
  return <div className="ai-chat-home">
    <header className="ai-chat-topbar">
      <button className="ai-chat-mobile-menu" onClick={props.onOpenHistory} aria-label="打开会话列表"><Menu size={20} /></button>
      <strong>AI 客户接待演练</strong>
      <span><ShieldCheck size={14} />沙盒</span>
    </header>
    <section className="ai-chat-empty">
      <div className="ai-chat-orb"><Sparkles size={30} /></div>
      <h1>模拟一个新客户</h1>
      <p>点击开始后，AI 会像真实客服一样开场、识别线路、回答问题，并在客户沉默时继续跟进，直到取得联系方式或转人工。</p>
      <label>客户首条消息<textarea value={props.entryMessage} onChange={event => props.onEntryChange(event.target.value)} rows={3} /></label>

      <button disabled={props.loading || !props.inboxName} onClick={props.onCreate}><Plus size={18} />{props.loading ? '正在准备...' : '开始新客户演练'}</button>
      <small>{props.inboxName || '正在读取渠道'} · 沙盒演练，不联系真实客户</small>
    </section>
  </div>;
}

function RehearsalRunner(props: {
  session: JourneySession;
  processing: boolean;
  busy: boolean;
  draft: string;
  setDraft: (value: string) => void;
  onSubmit: (event: FormEvent) => void;
  onSendText: (content: string) => void;
  onDelete: () => void;
  onCreate: () => void;
  onOpenHistory: () => void;
  onAdvanceNext: () => void;
  streamRef: React.RefObject<HTMLDivElement | null>;
}) {
  const { session } = props;
  const [detailsOpen, setDetailsOpen] = useState(false);
  const running = session.simulation.status === 'running';
  const latestRun = session.runs[0];
  const latestSilence = session.runs.find(run => run.module === 'silence_touch' && run.status === 'completed');
  const leadStatus = session.controls.lead_capture?.status ?? 'not_started';
  const nextJob = session.jobs.find(job => ['scheduled', 'waiting_dependency', 'model_pending'].includes(job.status));
  const currentStage = session.reception_state?.journey_stage ?? session.controls.journey?.stage ?? latestRun?.decision.journey_stage ?? 'route_selection';
  const lastTouch = session.reception_state?.last_touch ?? latestSilence?.decision;
  const timeline = useMemo(() => [
    ...session.messages.filter(customerTranscriptMessage).map((item, index) => ({ ...item, kind: 'message' as const, sort: item.timeline_sequence ?? 10000 + index })),
    ...(session.controls.timeline_events ?? []).map((item, index) => ({ ...item, kind: 'event' as const, sort: item.timeline_sequence ?? 20000 + index })),
  ].sort((left, right) => new Date(left.created_at).getTime() - new Date(right.created_at).getTime() || left.sort - right.sort), [session.messages, session.controls.timeline_events]);

  return <div className="ai-chat-conversation">
    <header className="ai-chat-topbar">
      <div className="ai-chat-title">
        <button className="ai-chat-mobile-menu" onClick={props.onOpenHistory} aria-label="打开会话列表"><Menu size={20} /></button>
        <div><strong>模拟客户 #{session.id}</strong><small>{session.engine_version.toUpperCase()} · {routeName(session.controls.route_variant)} · {props.processing ? 'AI 正在回复' : '等待客户'}</small><small>演练 · {session.engine_release_id}</small></div>
      </div>
      <div className="ai-chat-topbar-actions">
        <span className="ai-chat-safe"><ShieldCheck size={13} />沙盒</span>
        <button title="立即测试下一次沉默跟进" disabled={props.busy || props.processing || !running || (!nextJob && !session.reception_state?.next_touch_at)} onClick={props.onAdvanceNext}><FastForward size={17} /><span>测试下一次跟进</span></button>
        <button onClick={() => setDetailsOpen(value => !value)}><PanelRight size={17} /><span>接待状态</span></button>
        <button title="新客户演练" disabled={props.busy} onClick={props.onCreate}><Plus size={17} /></button>
        <button className="danger" title="删除演练" disabled={props.busy} onClick={props.onDelete}><Trash2 size={16} /></button>
      </div>
    </header>

    <div className="ai-chat-scroll" ref={props.streamRef}>
      <div className="ai-chat-transcript">
        {timeline.map(item => item.kind === 'event'
          ? <div className={`rehearsal-event ${item.event_type}`} key={item.id}>
            <span>{EVENT_LABELS[item.event_type] ?? '系统事件'}</span><p>{item.content}</p><time>{formatTime(item.created_at)}</time>
          </div>
          : <RehearsalMessage key={`${item.id}-${item.sort}`} message={item} running={running} onSelect={props.onSendText} />)}
        {props.processing ? <div className="rehearsal-thinking"><Bot size={16} /><span>AI 正在阅读对话与线路资料...</span></div> : null}
      </div>
    </div>

    <div className="ai-chat-composer-wrap">
      <div className="ai-chat-suggestions">
        {QUICK_MESSAGES.slice(0, 4).map(message => <button key={message} disabled={!running || props.busy} onClick={() => props.onSendText(message)}>{message}</button>)}
      </div>
      <form className="rehearsal-composer" onSubmit={props.onSubmit}>
        <textarea value={props.draft} onChange={event => props.setDraft(event.target.value)} onKeyDown={event => {
          if (event.key === 'Enter' && !event.shiftKey) {
            event.preventDefault();
            if (props.draft.trim() && running && !props.busy) props.onSendText(props.draft);
          }
        }} placeholder="以客户身份发送消息" disabled={!running || props.busy} rows={1} maxLength={4000} />
        <button type="submit" disabled={!running || !props.draft.trim() || props.busy} title="发送客户消息"><Send size={17} /></button>
      </form>
      <small>这是沙盒演练，消息不会发送给真实客户</small>
    </div>

    {detailsOpen ? <aside className="ai-chat-details">
      <header><div><strong>接待状态</strong><small>系统自动判断，客户无需选择</small></div><button onClick={() => setDetailsOpen(false)} aria-label="关闭接待状态"><X size={18} /></button></header>
      <section className="delivery-audit"><h3>消息投递审计</h3>{session.messages.filter(message => message.direction === 'outgoing').map(message => <details key={message.id}>
        <summary><span>{formatTime(message.created_at)} · {String(message.id)}</span><DeliveryStatus status={message.status} /></summary>
        {message.content ? <p>{message.content}</p> : null}
        {message.media_id ? <p>{message.content_type || '附件'} #{message.media_id}</p> : null}
        <DeliveryDetails status={message.status} attributes={message.content_attributes} record={message} />
        {message.reason ? <p>{sopReason(message.reason)}</p> : null}
      </details>)}</section>
      <section><h3>当前进度</h3><dl>
        <div><dt>识别线路</dt><dd>{routeName(session.controls.route_variant)}</dd></div>
        <div><dt>接待阶段</dt><dd>{JOURNEY_STAGE_LABELS[currentStage] ?? currentStage}</dd></div>
        <div><dt>接待状态</dt><dd>{session.controls.human ? '已转人工' : statusLabel(session.simulation.status)}</dd></div>
        <div><dt>联系方式</dt><dd>{leadStatus === 'captured' ? '已获得' : leadStatus === 'asked' ? '已索取，等待客户提供' : '尚未索取'}</dd></div>
        <div><dt>下一次触达</dt><dd>{nextJob ? `${formatTime(nextJob.scheduled_at)} · ${statusLabel(nextJob.status)}` : '当前无待执行跟进'}</dd></div>
      </dl></section>
      <section><h3>客户画像</h3>{Object.keys(session.memory).length ? <dl>{Object.entries(session.memory).filter(([key]) => key !== '_profile_meta').map(([key, value]) => <div key={key}><dt>{MEMORY_LABELS[key] ?? key}</dt><dd>{String(value.value)}{value.quote ? <small>客户原话：{value.quote}</small> : null}{'reason' in value && value.reason ? <small>AI 判断：{String(value.reason)}</small> : null}</dd></div>)}</dl> : <p>AI 会自动记录线路、人数、日期、首次进藏、手续认知、顾虑和决策状态，客户不需要填写表单。</p>}</section>
      <section><h3><CalendarClock size={14} />沉默跟进旅程</h3>{session.jobs.length ? <ol className="ai-chat-job-list">{session.jobs.map((job, index) => <li key={job.id}><span>{['simulated_delivered', 'already_provided', 'skipped', 'skipped_model_failure'].includes(job.status) ? <CheckCircle2 size={14} /> : index + 1}</span><div><strong>{sopNodeLabel(job.node_key)}</strong><small>{formatTime(job.scheduled_at)} · {statusLabel(job.status)}</small>{job.payload?.model_decision?.touch_goal ? <em>{TOUCH_GOAL_LABELS[job.payload.model_decision.touch_goal] ?? job.payload.model_decision.touch_goal}</em> : job.reason ? <em>{sopReason(job.reason)}</em> : null}</div></li>)}</ol> : <p>系统按已配置间隔检查旅程，只在还有相关新价值时生成跟进；没有新内容会安全跳过。</p>}{session.reception_state?.last_warning ? <p className="automation-error">{session.reception_state.last_warning}</p> : null}</section>
      <section><h3>最近一次沉默触达</h3>{lastTouch?.touch_goal ? <dl><div><dt>触达目标</dt><dd>{TOUCH_GOAL_LABELS[lastTouch.touch_goal] ?? lastTouch.touch_goal}</dd></div><div><dt>选择原因</dt><dd>{lastTouch.touch_reason || '-'}</dd></div><div><dt>触达后阶段</dt><dd>{JOURNEY_STAGE_LABELS[lastTouch.journey_stage || ''] ?? lastTouch.journey_stage ?? '-'}</dd></div></dl> : <p>客户尚未到达第一个沉默触达时间点。</p>}</section>
      <section><h3>最近一次 AI 判断</h3>{latestRun ? <><PresentationSummary presentations={latestRun.decision.presentations} /><dl>
        <div><dt>处理结果</dt><dd>{latestRun.decision.action === 'handoff' ? '转人工' : latestRun.decision.action === 'no_action' ? '未执行 (no_action)' : latestRun.decision.action === 'reply' ? '生成回复决策' : '未知'}</dd></div>
        <div><dt>运行校验</dt><dd><RuntimeDetails run={latestRun} /></dd></div>
        <div><dt>线路</dt><dd>{routeName(latestRun.decision.route_variant)}</dd></div>
        <div><dt>留资动作</dt><dd>{latestRun.decision.lead_action === 'ask' ? '已询问联系方式' : latestRun.decision.lead_action === 'captured' ? '已获得联系方式' : '暂不询问'}</dd></div>
        <div><dt>处理耗时</dt><dd>{latestRun.trace.total_ms ? `${(latestRun.trace.total_ms / 1000).toFixed(2)} 秒` : '-'}</dd></div>
      </dl></> : <p>等待第一轮 AI 回复。</p>}</section>
      <section className="ai-chat-test-prompts"><h3>测试例句</h3>{QUICK_MESSAGES.map(message => <button key={message} disabled={!running || props.busy} onClick={() => { props.onSendText(message); setDetailsOpen(false); }}>{message}</button>)}</section>
      <footer><ShieldCheck size={15} /><span><strong>不会触达真实客户</strong><small>固定 outbound=false</small></span></footer>
    </aside> : null}
  </div>;
}

function RehearsalMessage({ message, running, onSelect }: { message: SessionMessage & { sort: number }; running: boolean; onSelect: (value: string) => void }) {
  const outgoing = message.direction === 'outgoing';
  const options = hasOptionItems(message);
  return <div className={`rehearsal-message ${outgoing ? 'assistant' : 'customer'}`}>
    <span>{outgoing ? ['sop', 'sop_ai'].includes(message.source || '') ? <Workflow size={14} /> : <Bot size={14} /> : <UserRound size={14} />}</span>
    <div>
      <header><strong>{outgoing ? message.source === 'sop_ai' ? 'AI 沉默跟进' : message.source === 'sop' ? '固定 SOP' : 'AI 客服' : '模拟客户'}</strong><small>{formatTime(message.created_at)}</small></header>
      {message.content ? <p>{message.content}</p> : null}
      {!message.content && !message.media_id ? <p>[{message.content_type ?? '消息'}]</p> : null}
      {message.media_id && message.content_type === 'image'
        ? <RehearsalImage mediaId={message.media_id} />
        : null}
      {message.media_id && message.content_type === 'video'
        ? <RehearsalVideo mediaId={message.media_id} />
        : null}
      {options.length > 0 ? <div className="rehearsal-options">{options.map((item, index) => {
        const value = item.value || item.title || '';
        return <button key={`${value}-${index}`} disabled={!running || !value} onClick={() => onSelect(value)}>{item.title || value}</button>;
      })}</div> : null}
      <footer>{outgoing ? <span className="customer-delivery-status"><DeliveryStatus status={message.status} /></span> : <Badge>{message.status ? statusLabel(message.status) : '客户消息'}</Badge>}{message.content_type && message.content_type !== 'text' ? <span><ImageIcon size={12} />{message.content_type}</span> : null}</footer>
    </div>
  </div>;
}

function RehearsalImage({ mediaId }: { mediaId: number }) {
  const [state, setState] = useState<'loading' | 'ready' | 'failed'>('loading');
  const src = `${API_BASE}/media/${mediaId}/preview`;
  return <div className={`rehearsal-image-state ${state}`}>
    {state === 'loading' ? <span>图片加载中...</span> : null}
    {state === 'failed' ? <span>图片加载失败，可在运行审计中检查素材绑定。</span> : null}
    <a href={src} target="_blank" rel="noreferrer" hidden={state === 'failed'}>
      <img src={src} alt="线路素材" hidden={state !== 'ready'} onLoad={() => setState('ready')} onError={() => setState('failed')} />
    </a>
  </div>;
}

function RehearsalVideo({ mediaId }: { mediaId: number }) {
  const [failed, setFailed] = useState(false);
  return <div className="rehearsal-video">
    <video src={`${API_BASE}/media/${mediaId}/preview`} controls playsInline preload="metadata"
      onLoadedMetadata={() => setFailed(false)} onError={() => setFailed(true)}
      aria-label="开场视频" style={{ width: '100%', maxWidth: 650, maxHeight: 480, display: 'block' }} />
    {failed ? <p role="alert">视频无法播放，请检查文件格式或重新上传。</p> : null}
  </div>;
}
