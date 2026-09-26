import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Activity, ArrowDown, CheckCircle2, ChevronLeft, ChevronRight, ExternalLink, Filter, Headphones, Info, Plus, RotateCcw, Search, Tags, UserRoundCog } from 'lucide-react';
import { api, ApiError } from '../api';
import { Avatar, Badge, DetailRow, EmptyState, Modal, Toggle } from '../components';

import { DeliveryDetails } from '../DeliveryDetails';

interface ConversationSummary {
  id: number; name: string; channel: string; inbox: string; inbox_id: number; last_message: string;
  ai_state: string; ai_reason: string; labels: string[]; can_reply: boolean; status: string;
  ai_mode: 'inherit' | 'enabled' | 'disabled'; ai_mode_source: string; ai_label_present: boolean; ai_sync_status: 'synced' | 'pending' | 'conflict';
  version: number; updated_at: string; chatwoot_url: string;
}
interface ConversationDetail extends ConversationSummary {
  contact: { name: string; email?: string; phone_number?: string; pii_masked: boolean };
  latest_ai_reply?: { status: string; failure_kind: string; created_at: string; completed_at?: string; model_ms?: number; request_count?: number } | null;
  lead_capture?: { status: 'not_started' | 'asked' | 'captured'; request_count: number; requested_at?: string; captured_at?: string; captured_kinds: string[]; masked_values: Record<string, string>; label_sync_status: string };
}
interface MessageAttachment { id?: number; file_type?: string; content_type?: string; extension?: string; data_url?: string; thumb_url?: string; }
interface MessageQuickReply { title: string; value?: string; }
interface Message { id: number; direction: string; private: boolean; content_type: string; content: string; status?: string; attribution: string; attachments?: MessageAttachment[]; content_attributes?: { items?: MessageQuickReply[]; delivery_item?: Record<string, unknown> }; created_at: string; }
interface Agent { id: number; name: string; availability_status: string; role?: string; thumbnail?: string; }
interface ChatwootLabel { id: number; title: string; description: string; color: string; show_on_sidebar: boolean; }
interface ConversationControls {
  assignee: Agent | null;
  team: { id: number; name: string } | null;
  agents: Agent[];
  labels: ChatwootLabel[];
  conversation_labels: string[];
  ai_mode: 'inherit' | 'enabled' | 'disabled';
  ai_label_present: boolean;
  ai_sync_status: 'synced' | 'pending' | 'conflict';
  can_create_labels: boolean;
}
interface ConversationFilterOptions {
  inboxes: { id: number; name: string; channel: string }[];
  labels: { title: string; color: string }[];
  ai_states: string[];
}

function stateLabel(value: string) {
  return ({ AI_ACTIVE: 'AI 接管', HUMAN_HANDOFF: '人工接管', AI_PAUSED_LABEL: '标签关闭', AI_PAUSED_CONVERSATION: '会话关闭', AI_PAUSED_SYNC: '同步异常', AI_PAUSED_INBOX: 'Inbox 关闭', AI_PAUSED_GLOBAL: '全局关闭', CHANNEL_BLOCKED: '渠道阻断' } as Record<string, string>)[value] ?? value;
}
function stateTone(value: string): 'green' | 'amber' | 'neutral' | 'red' | 'blue' {
  if (value === 'AI_ACTIVE') return 'green';
  if (value === 'HUMAN_HANDOFF') return 'amber';
  if (value === 'CHANNEL_BLOCKED') return 'red';
  return 'neutral';
}
function leadStatusLabel(value?: string) { return ({ not_started: '未触发', asked: '已询问', captured: '已留资' } as Record<string, string>)[value ?? ''] ?? value ?? '未触发'; }
function contactKindLabel(value: string) { return ({ email: 'Email', phone: '电话', whatsapp: 'WhatsApp', line: 'LINE', wechat: '微信' } as Record<string, string>)[value] ?? value; }
function initials(name: string) { return name.trim().slice(0, 2).toUpperCase() || '?'; }
function displayTime(value: string) { const date = new Date(value); return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour: '2-digit', minute: '2-digit' }); }

function noReplyReason(conversation?: ConversationSummary, detail?: ConversationDetail) {
  if (!conversation) return null;
  const labels = new Set([...(conversation.labels ?? []), ...(detail?.labels ?? [])]);
  if (conversation.ai_reason === 'ai_opt_in_required' && !conversation.ai_label_present) return '当前接待范围要求 ai 标签：系统只同步客户消息，不会自动回复。';
  if (conversation.ai_sync_status !== 'synced') return 'ai 标签同步异常：为避免误发，自动回复已停止。';
  if (!conversation.can_reply) return 'can_reply=false：Chatwoot 或 Facebook 当前不允许自动回复。';
  if (conversation.ai_state === 'HUMAN_HANDOFF' || labels.has('人工接管')) return '人工接管：客服已接手，AI 和 SOP 不再发送。';
  if (conversation.ai_state === 'AI_PAUSED_LABEL') return '标签关闭：当前标签组合要求停止 AI。';
  if (conversation.ai_state === 'AI_PAUSED_CONVERSATION') return '会话关闭：当前 Chatwoot 会话不是可接待状态。';
  if (conversation.ai_state === 'AI_PAUSED_SYNC') return '同步异常：无法确认 Chatwoot 最新状态，自动回复暂停。';
  if (conversation.ai_state === 'AI_PAUSED_INBOX') return 'Inbox 关闭：该收件箱未开启 AI 接待。';
  if (conversation.ai_state === 'AI_PAUSED_GLOBAL') return '全局关闭：生产自动回复开关未开启。';
  if (conversation.ai_state === 'CHANNEL_BLOCKED') return '渠道阻断：Facebook 回复窗口关闭或渠道限制发送。';
  if (detail?.lead_capture?.status === 'captured') return '客户已留资：进入顾问跟进，不再自动推进。';
  if (labels.has('拒绝联系') || labels.has('黑名单')) return '客户拒绝联系或黑名单：停止主动触达。';
  return null;
}
function systemEventText(content: string) {
  const assigned = content.match(/^Assigned to (.+?) by (.+)$/i);
  if (assigned) return `分配客服：${assigned[1]}（${assigned[2]}）`;
  const unassigned = content.match(/^对话未被 (.+?) 分配$/);
  if (unassigned) return `取消客服分配：${unassigned[1]}`;
  const labelAdded = content.match(/^(.+?) 添加 (.+)$/);
  if (labelAdded) return `添加标签：${labelAdded[1]} 添加「${labelAdded[2]}」`;
  return content;
}
function attachmentKind(attachment: MessageAttachment) {
  const value = `${attachment.file_type ?? ''} ${attachment.content_type ?? ''} ${attachment.extension ?? ''}`.toLowerCase();
  if (/image|jpg|jpeg|png|gif|webp/.test(value)) return 'image';
  if (/video|mp4|mov|webm/.test(value)) return 'video';
  if (/audio|mp3|wav|ogg|m4a/.test(value)) return 'audio';
  return 'file';
}
function safeMediaUrl(value?: string) { return value && /^https?:\/\//i.test(value) ? value : ''; }
function MessageAttachmentView({ attachment }: { attachment: MessageAttachment }) {
  const [failed, setFailed] = useState(false);
  const kind = attachmentKind(attachment);
  const fullUrl = safeMediaUrl(attachment.data_url) || safeMediaUrl(attachment.thumb_url);
  const previewUrl = safeMediaUrl(attachment.thumb_url) || fullUrl;
  if (kind === 'image') {
    if (!previewUrl || failed) return <span className="attachment-fallback">[image]</span>;
    return <a className="message-image-link" href={fullUrl || previewUrl} target="_blank" rel="noreferrer" title="打开原图"><img src={previewUrl} alt="聊天图片" loading="lazy" decoding="async" onError={() => setFailed(true)} /></a>;
  }
  const label = `[${kind}]`;
  return fullUrl ? <a className="attachment-file" href={fullUrl} target="_blank" rel="noreferrer">{label}</a> : <span className="attachment-fallback">{label}</span>;
}

export default function Conversations({ notify }: { notify: (message: string) => void }) {
  const queryClient = useQueryClient();
  const [query, setQuery] = useState('');
  const [inboxId, setInboxId] = useState('');
  const [label, setLabel] = useState('');
  const [aiState, setAiState] = useState('');
  const [days, setDays] = useState('0');
  const [page, setPage] = useState(1);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [isAtBottom, setIsAtBottom] = useState(true);
  const [assignmentOpen, setAssignmentOpen] = useState(false);
  const [labelsOpen, setLabelsOpen] = useState(false);
  const [selectedAssignee, setSelectedAssignee] = useState('');
  const [pauseAiOnAssign, setPauseAiOnAssign] = useState(true);
  const [draftLabels, setDraftLabels] = useState<string[]>([]);
  const [baseLabels, setBaseLabels] = useState<string[]>([]);
  const [showCreateLabel, setShowCreateLabel] = useState(false);
  const [newLabelTitle, setNewLabelTitle] = useState('');
  const [newLabelDescription, setNewLabelDescription] = useState('');
  const [newLabelColor, setNewLabelColor] = useState('#1F93FF');
  const messageStreamRef = useRef<HTMLDivElement>(null);
  const previousConversationRef = useRef<number | undefined>(undefined);
  const stickToBottomRef = useRef(true);

  const pageSize = 25;
  const filterOptions = useQuery({ queryKey: ['conversation-filter-options'], queryFn: () => api<ConversationFilterOptions>('/conversations/filters'), staleTime: 60000 });
  const conversations = useQuery({
    queryKey: ['conversations', query, inboxId, label, aiState, days, page],
    queryFn: () => {
      const params = new URLSearchParams({ q: query, page: String(page), page_size: String(pageSize) });
      if (inboxId) params.set('inbox_id', inboxId);
      if (label) params.set('label', label);
      if (aiState) params.set('ai_state', aiState);
      if (days !== '0') params.set('days', days);
      return api<{ items: ConversationSummary[]; total: number; page: number; page_size: number }>(`/conversations?${params}`);
    },
    refetchInterval: 10000,
  });
  const selected = conversations.data?.items.find((item) => item.id === selectedId) ?? conversations.data?.items[0];
  useEffect(() => { const items = conversations.data?.items; if (items?.length && (!selectedId || !items.some((item) => item.id === selectedId))) setSelectedId(items[0].id); }, [conversations.data, selectedId]);
  const messages = useQuery({ queryKey: ['messages', selected?.id], queryFn: () => api<{ items: Message[] }>(`/conversations/${selected!.id}/messages`), enabled: Boolean(selected), refetchInterval: 10000 });
  const detail = useQuery({ queryKey: ['conversation-detail', selected?.id, selected?.version], queryFn: () => api<ConversationDetail>(`/conversations/${selected!.id}`), enabled: Boolean(selected), refetchInterval: 10000 });
  const controls = useQuery({
    queryKey: ['conversation-controls', selected?.id, selected?.version],
    queryFn: () => api<ConversationControls>(`/conversations/${selected!.id}/controls`),
    enabled: Boolean(selected),
    staleTime: 15000,
  });
  const lastMessageId = messages.data?.items.at(-1)?.id;
  const latestReply = detail.data?.latest_ai_reply;
  const replyIssue = latestReply?.status === 'failed'
    ? latestReply.failure_kind === 'model_timeout' ? '本轮 AI 回复超时，未自动重发，请人工跟进。' : '本轮 AI 回复失败，请人工检查。'
    : latestReply?.status === 'submission_unknown' ? '本轮消息提交结果待核实，请先检查 Chatwoot，避免重复发送。'
    : latestReply?.status === 'retrying' ? 'AI 服务暂时未响应，本轮正在重试。' : null;
  const currentNoReplyReason = noReplyReason(selected, detail.data);

  const assignmentMutation = useMutation({
    mutationFn: () => api(`/conversations/${selected!.id}/assignment`, {
      method: 'PUT',
      body: JSON.stringify({ assignee_id: selectedAssignee ? Number(selectedAssignee) : null, pause_ai: Boolean(selectedAssignee) && pauseAiOnAssign }),
    }),
    onSuccess: async () => {
      setAssignmentOpen(false);
      notify(selectedAssignee ? '客服分配已同步到 Chatwoot' : '已取消客服分配');
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['conversations'] }),
        queryClient.invalidateQueries({ queryKey: ['conversation-controls', selected?.id] }),
      ]);
    },
  });
  const handoffMutation = useMutation({ mutationFn: () => api('/handoffs', { method: 'POST', body: JSON.stringify({ conversation_id: selected!.id, reason_code: 'manual', reason_detail: '平台用户手工发起', priority: 'P2' }) }), onSuccess: async () => { notify('已创建人工接管任务'); await Promise.all([queryClient.invalidateQueries({ queryKey: ['handoffs'] }), queryClient.invalidateQueries({ queryKey: ['conversations'] })]); } });

  const aiModeMutation = useMutation({
    mutationFn: (enabled: boolean) => api(`/conversations/${selected!.id}/ai-mode`, { method: 'PUT', body: JSON.stringify({ enabled }) }),
    onSuccess: async (_, enabled) => {
      notify(enabled ? 'AI 接管已开启并同步到 Chatwoot' : 'AI 接管已关闭并同步到 Chatwoot');
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['conversations'] }),
        queryClient.invalidateQueries({ queryKey: ['conversation-detail', selected?.id] }),
        queryClient.invalidateQueries({ queryKey: ['conversation-controls', selected?.id] }),
      ]);
    },
  });

  const labelsMutation = useMutation({
    mutationFn: () => api(`/conversations/${selected!.id}/labels`, {
      method: 'PUT',
      body: JSON.stringify({ labels: draftLabels, base_labels: baseLabels }),
    }),
    onSuccess: async () => {
      setLabelsOpen(false);
      notify('会话标签已同步到 Chatwoot');
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['conversations'] }),
        queryClient.invalidateQueries({ queryKey: ['conversation-controls', selected?.id] }),
      ]);
    },
  });

  const createLabelMutation = useMutation({
    mutationFn: () => api<ChatwootLabel>('/labels', {
      method: 'POST',
      body: JSON.stringify({ title: newLabelTitle.trim(), description: newLabelDescription.trim(), color: newLabelColor, show_on_sidebar: true }),
    }),
    onSuccess: async (label) => {
      setDraftLabels((current) => current.includes(label.title) ? current : [...current, label.title]);
      setNewLabelTitle('');
      setNewLabelDescription('');
      setShowCreateLabel(false);
      notify('账号标签已创建，保存后应用到当前会话');
      await controls.refetch();
    },
  });

  useLayoutEffect(() => {
    const stream = messageStreamRef.current;
    if (!stream || !selected) return;
    const conversationChanged = previousConversationRef.current !== selected.id;
    if (conversationChanged) {
      previousConversationRef.current = selected.id;
      stickToBottomRef.current = true;
    }
    if (stickToBottomRef.current) {
      stream.scrollTop = stream.scrollHeight;
      setIsAtBottom(true);
    }
  }, [selected?.id, messages.data?.items.length, lastMessageId]);

  function handleMessageScroll() {
    const stream = messageStreamRef.current;
    if (!stream) return;
    const atBottom = stream.scrollHeight - stream.scrollTop - stream.clientHeight < 56;
    stickToBottomRef.current = atBottom;
    setIsAtBottom(atBottom);
  }

  function scrollToLatest() {
    const stream = messageStreamRef.current;
    if (!stream) return;
    stickToBottomRef.current = true;
    stream.scrollTo({ top: stream.scrollHeight, behavior: 'smooth' });
    setIsAtBottom(true);
  }

  function openAssignment() {
    setSelectedAssignee(controls.data?.assignee?.id ? String(controls.data.assignee.id) : '');
    setPauseAiOnAssign(true);
    assignmentMutation.reset();
    setAssignmentOpen(true);
  }

  function openLabels() {
    const current = controls.data?.conversation_labels ?? selected?.labels ?? [];
    setDraftLabels([...current]);
    setBaseLabels([...current]);
    setShowCreateLabel(false);
    labelsMutation.reset();
    createLabelMutation.reset();
    setLabelsOpen(true);
  }

  function toggleDraftLabel(title: string) {
    setDraftLabels((current) => current.includes(title) ? current.filter((item) => item !== title) : [...current, title]);
  }

  const assignmentError = assignmentMutation.error as ApiError | null;
  const labelsError = (labelsMutation.error ?? createLabelMutation.error) as ApiError | null;
  const totalPages = Math.max(1, Math.ceil((conversations.data?.total ?? 0) / pageSize));
  const hasFilters = Boolean(query || inboxId || label || aiState || days !== '0');

  function resetFilters() {
    setQuery(''); setInboxId(''); setLabel(''); setAiState(''); setDays('0'); setPage(1);
  }

  function renderMessage(message: Message) {
    if (message.direction === 'activity' || message.attribution === 'system') {
      const isLabelEvent = message.content.includes(' 添加 ');
      const EventIcon = isLabelEvent ? Tags : message.content.toLowerCase().includes('assigned') || message.content.includes('分配') ? UserRoundCog : Activity;
      return <div className="system-event" key={message.id}><span className="system-event-icon"><EventIcon size={14} /></span><div><strong>系统动态</strong><span>{systemEventText(message.content)}</span></div><time>{displayTime(message.created_at)}</time></div>;
    }
    const outgoing = message.direction === 'outgoing';
    const actor = !outgoing ? initials(selected?.name ?? '') : message.attribution === 'ai' ? 'AI' : message.attribution === 'sop' ? 'SOP' : '人';
    const source = !outgoing ? '客户' : message.attribution === 'ai' ? 'AI' : message.attribution === 'sop' ? 'SOP' : message.private ? '私密备注' : '人工';
    const sourceClass = !outgoing ? 'customer' : message.attribution === 'ai' ? 'ai' : message.attribution === 'sop' ? 'sop' : message.private ? 'private' : 'human';
    const attachments = message.attachments ?? [];
    const placeholderOnly = attachments.length > 0 && /^\[(图片|视频|音频|文件|image|video|audio|file|text)\]$/i.test(message.content.trim());
    const content = !placeholderOnly && message.content ? message.content : attachments.length ? '' : ({ image: '[image]', video: '[video]', audio: '[audio]', file: '[file]' } as Record<string, string>)[message.content_type] || `[${message.content_type}]`;
    const quickReplies = message.content_type === 'input_select' ? message.content_attributes?.items ?? [] : [];
    return <div className={`message-row ${outgoing ? 'outgoing' : 'incoming'} ${message.private ? 'private-message' : ''} attribution-${message.attribution}`} key={message.id}><Avatar initials={actor} tone={message.attribution === 'ai' ? 4 : outgoing ? 1 : 2} size="sm" /><div><div className={`message-bubble ${attachments.length ? 'with-attachments' : ''}`}>{attachments.length ? <div className="message-attachments">{attachments.map((attachment, index) => <MessageAttachmentView key={attachment.id ?? index} attachment={attachment} />)}</div> : null}{content ? <div className="message-text">{content}</div> : null}{quickReplies.length ? <div className="message-quick-replies" aria-label="Messenger 快捷回复选项">{quickReplies.map((item) => <span key={item.value ?? item.title}>{item.title}</span>)}</div> : null}</div><div className="message-meta"><span className={`message-source source-${sourceClass}`}>{source}</span><time>{displayTime(message.created_at)}</time>{outgoing || message.status ? <DeliveryDetails status={message.status} attributes={message.content_attributes} record={message} /> : null}</div></div></div>;
  }

  return <div className="page-content conversation-page">
    <section className="conversation-filterbar" aria-label="会话筛选">
      <div className="filterbar-title"><Filter size={16} /><strong>客户筛选</strong></div>
      <div className="conversation-filter-search"><Search size={16} /><input value={query} onChange={(event) => { setQuery(event.target.value); setPage(1); }} placeholder="搜索联系人、会话 ID 或消息" /></div>
      <select aria-label="筛选收件箱" value={inboxId} onChange={(event) => { setInboxId(event.target.value); setPage(1); }}><option value="">全部 Inbox</option>{filterOptions.data?.inboxes.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select>
      <select aria-label="筛选标签" value={label} onChange={(event) => { setLabel(event.target.value); setPage(1); }}><option value="">全部标签</option>{filterOptions.data?.labels.map((item) => <option key={item.title} value={item.title}>{item.title}</option>)}</select>
      <select aria-label="筛选 AI 状态" value={aiState} onChange={(event) => { setAiState(event.target.value); setPage(1); }}><option value="">全部 AI 状态</option>{filterOptions.data?.ai_states.map((item) => <option key={item} value={item}>{stateLabel(item)}</option>)}</select>
      <select aria-label="筛选更新时间" value={days} onChange={(event) => { setDays(event.target.value); setPage(1); }}><option value="0">全部时间</option><option value="1">最近 24 小时</option><option value="7">最近 7 天</option><option value="30">最近 30 天</option><option value="90">最近 90 天</option></select>
      {hasFilters ? <button className="icon-button filter-reset" onClick={resetFilters} title="清除筛选" aria-label="清除筛选"><RotateCcw size={16} /></button> : null}
      <span className="filter-result">{conversations.data?.total ?? 0} 个会话</span>
    </section>
    <section className="conversation-shell">
      <aside className="conversation-list-panel">
        <div className="list-summary"><span>{conversations.data?.total ?? 0} 个会话</span><span>第 {page} / {totalPages} 页</span></div>
        <div className="conversation-list">
          {conversations.isLoading ? <EmptyState type="loading" title="正在加载会话" description="读取本地会话镜像。" /> : conversations.isError ? <EmptyState type="error" title="无法读取会话" description={(conversations.error as ApiError).message} action={<button className="secondary-button" onClick={() => conversations.refetch()}>重试</button>} /> : !conversations.data?.items.length ? <EmptyState title="暂无真实会话" description="配置 Chatwoot Webhook 后，新的消息会显示在这里。" /> : conversations.data.items.map((item, index) => <button key={item.id} className={`conversation-card ${item.id === selected?.id ? 'selected' : ''}`} onClick={() => setSelectedId(item.id)}><Avatar initials={initials(item.name)} tone={index} /><div className="conversation-card-body"><div><strong>{item.name}</strong><time>{displayTime(item.updated_at)}</time></div><p>{item.last_message || '暂无消息正文'}</p><footer><Badge tone={stateTone(item.ai_state)}>{stateLabel(item.ai_state)}</Badge><span>{item.channel} · #{item.id}</span></footer></div></button>)}
        </div>
        <footer className="conversation-pagination"><button className="icon-button" aria-label="上一页会话" disabled={page <= 1} onClick={() => setPage((value) => Math.max(1, value - 1))}><ChevronLeft size={17} /></button><span>{pageSize} 条/页 · 10 秒刷新</span><button className="icon-button" aria-label="下一页会话" disabled={page >= totalPages} onClick={() => setPage((value) => Math.min(totalPages, value + 1))}><ChevronRight size={17} /></button></footer>
      </aside>

      <main className="chat-panel">
        {!selected ? <EmptyState title="选择一个会话" description="Webhook 接收到的真实会话会显示在左侧。" /> : <>
          <header className="chat-header"><div className="chat-identity"><Avatar initials={initials(selected.name)} tone={2} size="lg" /><div><div><strong title={selected.name}>{selected.name}</strong><Badge tone={stateTone(selected.ai_state)}>{stateLabel(selected.ai_state)}</Badge></div><span>{selected.channel} · {selected.inbox} · #{selected.id}</span></div></div><div className="chat-actions"><button className="secondary-button chat-command-button" onClick={() => handoffMutation.mutate()} disabled={handoffMutation.isPending || selected.ai_state !== 'AI_ACTIVE'} title="转人工"><Headphones size={16} /><span>转人工</span></button><button className="secondary-button chat-command-button" onClick={openAssignment} title="分配客服" aria-label="分配客服"><UserRoundCog size={16} /><span>分配客服</span></button><button className="secondary-button chat-command-button" onClick={openLabels} title="设置标签" aria-label="设置标签"><Tags size={16} /><span>设置标签</span></button><button className="secondary-button icon-only" onClick={() => window.open(selected.chatwoot_url, '_blank', 'noopener,noreferrer')} title="打开 Chatwoot" aria-label="打开 Chatwoot"><ExternalLink size={16} /></button></div></header>
          {currentNoReplyReason ? <div className="warning-strip no-reply-reason" role="status"><Info size={16} /><span>{currentNoReplyReason}</span></div> : null}
          {!selected.can_reply ? <div className="warning-strip"><Info size={16} />渠道当前不可回复，Worker 不会发送自动回复。</div> : null}
          {replyIssue ? <div className="warning-strip reply-issue" role="status"><Info size={16} /><span>{replyIssue}</span><time>{displayTime(latestReply!.completed_at || latestReply!.created_at)}</time></div> : null}
          <div className="message-stream-wrap">
            <div className="message-stream" ref={messageStreamRef} onScroll={handleMessageScroll}>
              {messages.isLoading ? <EmptyState type="loading" title="正在加载消息" description="" /> : !messages.data?.items.length ? <EmptyState title="暂无本地消息" description="可在系统设置启动历史同步；后续 Webhook 消息会实时写入。" /> : messages.data.items.map(renderMessage)}
            </div>
            {!isAtBottom && messages.data?.items.length ? <button className="scroll-latest-button" onClick={scrollToLatest} title="回到最新消息" aria-label="回到最新消息"><ArrowDown size={17} /></button> : null}
          </div>
        </>}
      </main>

      <aside className="context-panel">
        {selected ? <>
          <header className="context-header"><span>CONTEXT</span><h2>会话情报</h2><p>来自 Chatwoot 与本平台事件镜像</p></header>
          <section><h3>AI 最终状态</h3><div className="context-ai-toggle"><div><strong>会话 AI 接管</strong><span>{selected.ai_mode === 'inherit' ? '继承 Inbox 策略' : selected.ai_sync_status === 'synced' ? '已与 Chatwoot 的 ai 标签同步' : '标签同步异常，已停止发送'}</span></div><Toggle checked={selected.ai_mode === 'enabled' || selected.ai_mode === 'inherit' && selected.ai_state === 'AI_ACTIVE'} onChange={(enabled) => aiModeMutation.mutate(enabled)} disabled={aiModeMutation.isPending || selected.ai_sync_status === 'pending'} label="切换会话 AI 接管" /></div>{aiModeMutation.isError ? <p className="negative-text context-action-error">{(aiModeMutation.error as ApiError).message}</p> : null}{currentNoReplyReason ? <div className="context-blocker"><Info size={15} /><span>{currentNoReplyReason}</span></div> : <div className="context-ready"><CheckCircle2 size={15} /><span>下一条客户消息满足条件时会触发 AI。</span></div>}<DetailRow label="状态"><Badge tone={stateTone(selected.ai_state)}>{stateLabel(selected.ai_state)}</Badge></DetailRow><DetailRow label="控制模式"><span>{selected.ai_mode === 'inherit' ? '继承' : selected.ai_mode === 'enabled' ? '开启' : '关闭'}</span></DetailRow><DetailRow label="标签同步"><span className={selected.ai_sync_status === 'synced' ? 'positive-text' : 'negative-text'}>{selected.ai_sync_status === 'synced' ? '正常' : selected.ai_sync_status === 'pending' ? '同步中' : '异常'}</span></DetailRow><DetailRow label="原因"><code>{selected.ai_reason}</code></DetailRow><DetailRow label="可回复"><span className={selected.can_reply ? 'positive-text' : 'negative-text'}>{selected.can_reply ? '允许' : '不允许'}</span></DetailRow><DetailRow label="版本"><span>v{selected.version}</span></DetailRow></section>
          <section><div className="context-section-title"><h3>负责客服</h3><button onClick={openAssignment}>修改</button></div>{controls.isLoading ? <span className="context-note">正在读取 Chatwoot...</span> : controls.isError ? <span className="negative-text">读取失败</span> : controls.data?.assignee ? <div className="assignee-summary"><Avatar initials={initials(controls.data.assignee.name)} size="sm" /><div><strong>{controls.data.assignee.name}</strong><span><i className={`availability-dot ${controls.data.assignee.availability_status}`} />{controls.data.assignee.availability_status}</span></div></div> : <span className="context-note">未分配客服</span>}</section>
          <section><div className="context-section-title"><h3>Chatwoot 标签</h3><button onClick={openLabels}>编辑</button></div><div className="tag-list">{selected.labels.length ? selected.labels.map((label) => { const definition = controls.data?.labels.find((item) => item.title === label); return <span className="conversation-tag" key={label}><i style={{ backgroundColor: definition?.color ?? '#64748B' }} />{label}</span>; }) : <span className="context-note">暂无标签</span>}</div></section>
          <section><h3>客户联系方式</h3><DetailRow label="留资状态"><Badge tone={detail.data?.lead_capture?.status === 'captured' ? 'green' : detail.data?.lead_capture?.status === 'asked' ? 'amber' : 'neutral'}>{leadStatusLabel(detail.data?.lead_capture?.status)}</Badge></DetailRow><DetailRow label="邮箱"><span>{detail.data?.contact.email ?? '未提供'}</span></DetailRow><DetailRow label="电话"><span>{detail.data?.contact.phone_number ?? '未提供'}</span></DetailRow>{(detail.data?.lead_capture?.captured_kinds ?? []).map((kind) => detail.data?.lead_capture?.masked_values[kind] ? <DetailRow key={kind} label={contactKindLabel(kind)}><span>{detail.data.lead_capture.masked_values[kind]}</span></DetailRow> : null)}{detail.data?.lead_capture?.status === 'asked' ? <p className="context-note">AI 已询问一次联系方式，后续不会重复索取。</p> : null}{detail.data?.lead_capture?.label_sync_status === 'failed' ? <p className="negative-text context-action-error">留资标签同步失败，平台已强制转人工。</p> : null}{detail.data?.contact.pii_masked ? <p className="context-note">当前会话尚未分配给你，敏感信息已脱敏。</p> : null}</section>
          <section><h3>数据来源</h3><DetailRow label="Conversation"><code>{selected.id}</code></DetailRow><DetailRow label="Inbox"><code>{selected.inbox_id}</code></DetailRow><p className="context-note">客服分配和标签写入 Chatwoot；Webhook 会将人工侧修改同步回本平台。</p></section>
        </> : null}
      </aside>
    </section>

    <Modal open={assignmentOpen} title="分配客服" description={`当前会话 #${selected?.id ?? ''}，仅显示该 Inbox 的可用成员。`} onClose={() => setAssignmentOpen(false)} footer={<><button className="secondary-button" onClick={() => setAssignmentOpen(false)}>取消</button><button className="primary-button" onClick={() => assignmentMutation.mutate()} disabled={assignmentMutation.isPending || controls.isLoading}>{assignmentMutation.isPending ? '正在同步...' : '确认分配'}</button></>}>
      <div className="form-stack"><label>负责客服<select value={selectedAssignee} onChange={(event) => setSelectedAssignee(event.target.value)}><option value="">未分配</option>{controls.data?.agents.map((agent) => <option value={agent.id} key={agent.id}>{agent.name} · {agent.availability_status}</option>)}</select></label>{selectedAssignee ? <div className="switch-setting"><div><strong>同时停止 AI</strong><span>先添加“人工接管”标签，再分配客服，避免交接期间自动回复。</span></div><Toggle checked={pauseAiOnAssign} onChange={setPauseAiOnAssign} label="同时添加人工接管标签" /></div> : <div className="info-box"><Info size={16} /><span>取消分配不会自动删除“人工接管”标签，恢复 AI 需要单独编辑标签。</span></div>}{assignmentError ? <div className="login-error">{assignmentError.message}</div> : null}</div>
    </Modal>

    <Modal open={labelsOpen} title="设置会话标签" description="会与 Chatwoot 当前标签做差量合并，避免覆盖其他客服刚添加的标签。" onClose={() => setLabelsOpen(false)} footer={<><button className="secondary-button" onClick={() => setLabelsOpen(false)}>取消</button><button className="primary-button" onClick={() => labelsMutation.mutate()} disabled={labelsMutation.isPending || controls.isLoading}>{labelsMutation.isPending ? '正在同步...' : '保存标签'}</button></>}>
      <div className="label-picker">
        {controls.data?.labels.map((label) => <label key={label.id} className={draftLabels.includes(label.title) ? 'selected' : ''}><input type="checkbox" checked={draftLabels.includes(label.title)} onChange={() => toggleDraftLabel(label.title)} /><i style={{ backgroundColor: label.color }} /><span><strong>{label.title}</strong><small>{label.description || '无说明'}</small></span></label>)}
        {!controls.isLoading && !controls.data?.labels.length ? <div className="info-box"><Info size={16} /><span>Chatwoot 账号中尚无标签。</span></div> : null}
      </div>
      {controls.data?.can_create_labels ? <div className="create-label-block">{!showCreateLabel ? <button className="secondary-button" onClick={() => setShowCreateLabel(true)}><Plus size={15} />创建账号标签</button> : <div className="form-stack"><div className="create-label-heading"><strong>新建 Chatwoot 标签</strong><button className="text-button" onClick={() => setShowCreateLabel(false)}>收起</button></div><label>名称<input value={newLabelTitle} maxLength={100} onChange={(event) => setNewLabelTitle(event.target.value)} placeholder="例如：已留资" /></label><label>说明<input value={newLabelDescription} maxLength={500} onChange={(event) => setNewLabelDescription(event.target.value)} placeholder="说明标签使用场景" /></label><label>颜色<div className="color-field"><input type="color" value={newLabelColor} onChange={(event) => setNewLabelColor(event.target.value)} /><code>{newLabelColor.toUpperCase()}</code></div></label><button className="secondary-button" onClick={() => createLabelMutation.mutate()} disabled={!newLabelTitle.trim() || createLabelMutation.isPending}>{createLabelMutation.isPending ? '创建中...' : '创建并选中'}</button></div>}</div> : null}
      {labelsError ? <div className="login-error">{labelsError.message}</div> : null}
    </Modal>
  </div>;
}
