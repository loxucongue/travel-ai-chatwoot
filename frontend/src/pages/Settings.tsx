import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AlertTriangle, Bell, Bot, CheckCircle2, Database, KeyRound, Link2, RefreshCw, Save, ScrollText, ShieldCheck, Tags, Users, Webhook } from 'lucide-react';
import { api, ApiError } from '../api';
import { Badge, EmptyState, PageHeader, StatusDot, Toggle } from '../components';

type Section = 'channels' | 'webhooks' | 'ai' | 'rollout' | 'history' | 'mappings' | 'users' | 'notifications' | 'audit';
interface ChatwootConfig { configured: boolean; base_url: string; account_id: number; token_last4?: string; status?: string; last_tested_at?: string; last_error?: string; webhook_url?: string; }
interface Inbox { id: number; chatwoot_inbox_id: number; name: string; channel_type: string; ai_enabled: boolean; status: string; last_synced_at: string; }
interface AiReceptionRollout {
  allowlist_enabled: boolean;
  conversation_ids: number[];
  scope: 'allowlist' | 'ai_label';
  require_ai_label: true;
  global_message_sending_enabled: boolean;
  cancelled_reply_jobs?: number;
  cancelled_sop_jobs?: number;
  cancelled_enrollments?: number;
}
const eventOptions = ['message_created', 'message_updated', 'conversation_created', 'conversation_updated', 'conversation_status_changed', 'contact_created', 'contact_updated'];
const requiredEvents = new Set(['message_created', 'message_updated', 'conversation_updated', 'conversation_status_changed', 'contact_updated']);

export default function Settings({ notify }: { notify: (message: string) => void }) {
  const [section, setSection] = useState<Section>('channels');
  const items: { id: Section; label: string; icon: typeof Link2 }[] = [{ id: 'channels', label: '渠道与收件箱', icon: Link2 }, { id: 'webhooks', label: 'Account Webhook', icon: Webhook }, { id: 'history', label: '历史数据', icon: Database }, { id: 'ai', label: 'AI Adapter', icon: Bot }, { id: 'rollout', label: 'AI 接待范围', icon: ShieldCheck }, { id: 'mappings', label: '标签映射', icon: Tags }, { id: 'users', label: '用户与权限', icon: Users }, { id: 'notifications', label: '顾问通知', icon: Bell }, { id: 'audit', label: '审计日志', icon: ScrollText }];
  return <div className="page-content"><PageHeader eyebrow="SYSTEM CONFIGURATION" title="系统设置" description="管理 Chatwoot 连接、AI、权限、安全策略与历史数据。" /><section className="settings-shell"><nav className="settings-nav">{items.map((item) => { const Icon = item.icon; return <button key={item.id} className={section === item.id ? 'active' : ''} onClick={() => setSection(item.id)}><Icon size={17} /><span>{item.label}</span></button>; })}</nav><main className="settings-main">{section === 'channels' ? <Channels notify={notify} /> : section === 'webhooks' ? <WebhookSettings notify={notify} /> : section === 'history' ? <HistorySettings notify={notify} /> : section === 'ai' ? <AiSettings notify={notify} /> : section === 'rollout' ? <AiReceptionRolloutSettings notify={notify} /> : section === 'mappings' ? <MappingSettings notify={notify} /> : section === 'users' ? <UserSettings notify={notify} /> : section === 'notifications' ? <NotificationSettings notify={notify} /> : <AuditLogs />}</main></section></div>;
}

function parseConversationIds(value: string): number[] | null {
  const parts = value.split(/[\s,，]+/).map(item => item.trim()).filter(Boolean);
  if (parts.some(item => !/^\d+$/.test(item) || Number(item) <= 0)) return null;
  return Array.from(new Set(parts.map(Number))).sort((left, right) => left - right);
}

function AiReceptionRolloutSettings({ notify }: { notify: (value: string) => void }) {
  const queryClient = useQueryClient();
  const query = useQuery({ queryKey: ['ai-reception-rollout'], queryFn: () => api<AiReceptionRollout>('/settings/ai-reception-rollout') });
  const [allowlistEnabled, setAllowlistEnabled] = useState(true);
  const [conversationIds, setConversationIds] = useState('');
  const [scopeConfirmed, setScopeConfirmed] = useState(false);
  useEffect(() => {
    if (!query.data) return;
    setAllowlistEnabled(query.data.allowlist_enabled);
    setConversationIds(query.data.conversation_ids.join(', '));
    setScopeConfirmed(false);
  }, [query.data]);
  const parsedIds = parseConversationIds(conversationIds);
  const validationError = parsedIds === null
    ? '会话编号只能填写正整数，并使用逗号、空格或换行分隔。'
    : allowlistEnabled && parsedIds.length === 0
      ? '开启白名单时至少需要保留一个会话编号。'
      : !allowlistEnabled && !scopeConfirmed
        ? '关闭白名单前需要确认接待范围。'
        : '';
  const save = useMutation({
    mutationFn: () => api<AiReceptionRollout>('/settings/ai-reception-rollout', {
      method: 'PATCH',
      body: JSON.stringify({
        allowlist_enabled: allowlistEnabled,
        conversation_ids: parsedIds ?? [],
        confirm_ai_label_scope: !allowlistEnabled && scopeConfirmed,
      }),
    }),
    onSuccess: async data => {
      await queryClient.invalidateQueries({ queryKey: ['ai-reception-rollout'] });
      const cancelled = (data.cancelled_reply_jobs ?? 0) + (data.cancelled_sop_jobs ?? 0) + (data.cancelled_enrollments ?? 0);
      notify(cancelled ? `AI 接待范围已保存，并停止 ${cancelled} 个范围外任务` : 'AI 接待范围已保存');
    },
  });
  const dirty = !!query.data && (
    allowlistEnabled !== query.data.allowlist_enabled
    || JSON.stringify(parsedIds ?? []) !== JSON.stringify(query.data.conversation_ids)
  );
  const error = query.error || save.error;

  if (query.isLoading) return <EmptyState type="loading" title="正在读取 AI 接待范围" description="" />;
  if (!query.data) return <EmptyState type="error" title="接待范围读取失败" description={(error as Error)?.message ?? '无法读取配置'} />;

  return <>
    <header className="settings-section-head"><div><h2>AI 接待范围</h2><p>临时控制哪些 Chatwoot 会话允许进入 AI 接待；修改后立即生效，无需重启。</p></div><Badge tone={allowlistEnabled ? 'blue' : 'amber'}>{allowlistEnabled ? '白名单模式' : '全部 ai 标签会话'}</Badge></header>
    <article className="setting-card rollout-setting-card">
      <div className="global-toggle-row"><div><strong>启用会话白名单</strong><p>开启后，只有下方列出的会话编号可以接待。</p></div><Toggle checked={allowlistEnabled} onChange={value => { setAllowlistEnabled(value); setScopeConfirmed(false); }} label="切换 AI 接待白名单" disabled={save.isPending} /></div>
      {allowlistEnabled ? <div className="form-stack"><label>Chatwoot 会话编号<textarea rows={5} value={conversationIds} onChange={event => setConversationIds(event.target.value)} placeholder="例如：26, 42, 105" /><small>可使用逗号、空格或换行分隔。当前共 {parsedIds?.length ?? 0} 个会话。</small></label></div> : <div className="rollout-warning"><AlertTriangle size={18} /><div><strong>关闭后不再按会话编号拦截</strong><p>所有满足全局发送开关、精确 ai 标签、Inbox 状态、渠道窗口及人工接管保护的会话，都可以由 AI 接待。</p><label><input type="checkbox" checked={scopeConfirmed} onChange={event => setScopeConfirmed(event.target.checked)} />我确认将接待范围扩大到所有带 ai 标签的合格会话</label></div></div>}
      <div className="info-box"><ShieldCheck size={16} /><span><strong>ai 标签始终是硬门禁。</strong> 无论白名单是否开启，没有精确 <code>ai</code> 标签的会话都不会建立或发送自动回复。当前全局发送：{query.data.global_message_sending_enabled ? '已开启' : '已关闭'}。</span></div>
      {error ? <div className="login-error">{(error as ApiError).message}</div> : null}
      <div className="setting-actions"><span>{dirty ? '存在未保存修改' : '当前配置已生效'}</span><button className="primary-button" onClick={() => save.mutate()} disabled={!dirty || !!validationError || save.isPending}><Save size={15} />{save.isPending ? '保存中…' : '保存接待范围'}</button></div>
      {validationError && dirty ? <div className="login-error">{validationError}</div> : null}
    </article>
  </>;
}

function Channels({ notify }: { notify: (value: string) => void }) {
  const queryClient = useQueryClient();
  const config = useQuery({ queryKey: ['chatwoot-config'], queryFn: () => api<ChatwootConfig>('/settings/chatwoot') });
  const inboxes = useQuery({ queryKey: ['inboxes'], queryFn: () => api<Inbox[]>('/settings/inboxes') });
  const [baseUrl, setBaseUrl] = useState('https://app.chatwoot.com'); const [accountId, setAccountId] = useState(180474); const [token, setToken] = useState('');
  useEffect(() => { if (config.data) { setBaseUrl(config.data.base_url); setAccountId(config.data.account_id); } }, [config.data]);
  const save = useMutation({ mutationFn: () => api<ChatwootConfig>('/settings/chatwoot', { method: 'PUT', body: JSON.stringify({ base_url: baseUrl, account_id: accountId, api_token: token || null }) }), onSuccess: () => { setToken(''); queryClient.invalidateQueries({ queryKey: ['chatwoot-config'] }); notify('Chatwoot 连接配置已加密保存'); } });
  const test = useMutation({ mutationFn: () => api<{ inbox_count: number }>('/settings/chatwoot/test', { method: 'POST' }), onSuccess: (data) => { queryClient.invalidateQueries({ queryKey: ['chatwoot-config'] }); notify(`连接正常，读取到 ${data.inbox_count} 个 Inbox`); } });
  const sync = useMutation({ mutationFn: () => api<{ inboxes: number }>('/settings/chatwoot/sync', { method: 'POST' }), onSuccess: (data) => { queryClient.invalidateQueries({ queryKey: ['inboxes'] }); notify(`已同步 ${data.inboxes} 个 Inbox`); } });
  const toggle = useMutation({ mutationFn: ({ id, enabled }: { id: number; enabled: boolean }) => api(`/settings/inboxes/${id}`, { method: 'PATCH', body: JSON.stringify({ ai_enabled: enabled }) }), onSuccess: () => queryClient.invalidateQueries({ queryKey: ['inboxes'] }) });
  const error = save.error || test.error || sync.error;

  return <><header className="settings-section-head"><div><h2>Chatwoot 连接</h2><p>Token 仅发送到本地 FastAPI，并以加密字段保存。</p></div><Badge tone={config.data?.status === 'connected' ? 'green' : 'amber'}><StatusDot tone={config.data?.status === 'connected' ? 'green' : 'amber'} />{config.data?.status ?? '未配置'}</Badge></header><article className="setting-card"><div className="form-stack compact-form"><label>Base URL<input value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} /></label><label>Account ID<input type="number" value={accountId} onChange={(event) => setAccountId(Number(event.target.value))} /></label><label>API Token<input type="password" value={token} onChange={(event) => setToken(event.target.value)} placeholder={config.data?.configured ? `已配置 ····${config.data.token_last4}` : '输入服务账号 Token'} /></label></div>{error ? <div className="login-error">{(error as ApiError).message}</div> : null}<div className="setting-actions"><button className="primary-button" onClick={() => save.mutate()} disabled={save.isPending}><Save size={15} />保存</button><button className="secondary-button" onClick={() => test.mutate()} disabled={!config.data?.configured || test.isPending}><CheckCircle2 size={15} />只读测试</button><button className="secondary-button" onClick={() => sync.mutate()} disabled={!config.data?.configured || sync.isPending}><RefreshCw size={15} />同步资源</button></div></article><article className="panel inbox-panel"><header className="panel-header"><div><h2>收件箱 AI 开关</h2><p>关闭只影响本平台自动回复，不影响 Chatwoot 人工收发。</p></div></header>{inboxes.isLoading ? <EmptyState type="loading" title="正在读取 Inbox" description="" /> : !inboxes.data?.length ? <EmptyState title="尚未同步 Inbox" description="保存连接后执行同步资源。" /> : <div className="responsive-table"><table><thead><tr><th>收件箱</th><th>渠道</th><th>Inbox ID</th><th>状态</th><th>AI 自动回复</th></tr></thead><tbody>{inboxes.data.map((item) => <tr key={item.id}><td><strong>{item.name}</strong></td><td>{item.channel_type}</td><td><code>{item.chatwoot_inbox_id}</code></td><td><Badge tone="green">{item.status}</Badge></td><td><Toggle checked={item.ai_enabled} onChange={(enabled) => toggle.mutate({ id: item.id, enabled })} label={`切换 ${item.name} AI`} disabled={toggle.isPending} /></td></tr>)}</tbody></table></div>}</article></>;
}

function WebhookSettings({ notify }: { notify: (value: string) => void }) {
  const query = useQuery({ queryKey: ['webhook'], queryFn: () => api<{ webhook_id?: number; url: string; events: string[]; last_received_at?: string }>('/settings/chatwoot/webhook') });
  const [events, setEvents] = useState(eventOptions);
  useEffect(() => { if (query.data?.events.length) setEvents(query.data.events); }, [query.data]);
  const save = useMutation({ mutationFn: () => api('/settings/chatwoot/webhook', { method: 'PUT', body: JSON.stringify({ events }) }), onSuccess: () => { query.refetch(); notify('Account Webhook 已保存到 Chatwoot'); } });
  return <><header className="settings-section-head"><div><h2>Account Webhook</h2><p>账号级事件入口，不属于单个客服。</p></div><Badge tone={query.data?.webhook_id ? 'green' : 'amber'}>{query.data?.webhook_id ? '已创建' : '待创建'}</Badge></header><article className="setting-card webhook-card"><label className="field-label">回调地址</label><code className="webhook-url">{query.data?.url ?? '请先配置 Chatwoot'}</code><div className="webhook-events"><span>订阅事件</span><div>{eventOptions.map((event) => <label key={event}><input type="checkbox" checked={events.includes(event)} disabled={requiredEvents.has(event)} onChange={() => setEvents((current) => current.includes(event) ? current.filter((value) => value !== event) : [...current, event])} /><span><strong>{event}</strong>{requiredEvents.has(event) ? ' · 必选' : ''}</span></label>)}</div></div>{save.error ? <div className="login-error">{(save.error as ApiError).message}</div> : null}<div className="setting-actions"><button className="primary-button" onClick={() => save.mutate()} disabled={save.isPending}><Save size={15} />保存 Webhook</button><span>最近接收：{query.data?.last_received_at ?? '尚无事件'}</span></div></article></>;
}

function AiSettings({ notify }: { notify: (value: string) => void }) {
  const query = useQuery({ queryKey: ['ai-config'], queryFn: () => api<{ trigger_text: string; reply_text: string }>('/settings/ai') });
  const adapterQuery = useQuery({ queryKey: ['ai-adapter'], queryFn: () => api<{ adapter: 'mock' | 'http'; enabled: boolean; url?: string; timeout_seconds: number; failure_handoff_threshold: number; token_configured: boolean }>('/settings/ai-adapter') });
  const [trigger, setTrigger] = useState('测试人员触发消息'); const [reply, setReply] = useState('测试人员回复消息');
  const [adapter, setAdapter] = useState<'mock' | 'http'>('mock'); const [enabled, setEnabled] = useState(true); const [url, setUrl] = useState(''); const [token, setToken] = useState(''); const [timeout, setTimeoutValue] = useState(15); const [threshold, setThreshold] = useState(3);
  useEffect(() => { if (query.data) { setTrigger(query.data.trigger_text); setReply(query.data.reply_text); } }, [query.data]);
  useEffect(() => { if (adapterQuery.data) { setAdapter(adapterQuery.data.adapter); setEnabled(adapterQuery.data.enabled); setUrl(adapterQuery.data.url ?? ''); setTimeoutValue(adapterQuery.data.timeout_seconds); setThreshold(adapterQuery.data.failure_handoff_threshold); } }, [adapterQuery.data]);
  const save = useMutation({ mutationFn: () => api('/settings/ai', { method: 'PUT', body: JSON.stringify({ trigger_text: trigger, reply_text: reply }) }), onSuccess: () => notify('Mock AI 规则已保存') });
  const saveAdapter = useMutation({ mutationFn: () => api('/settings/ai-adapter', { method: 'PATCH', body: JSON.stringify({ adapter, enabled, url: url || null, bearer_token: token || null, timeout_seconds: timeout, failure_handoff_threshold: threshold }) }), onSuccess: () => { setToken(''); adapterQuery.refetch(); notify('AI Adapter 已保存'); } });
  return <><header className="settings-section-head"><div><h2>AI Adapter</h2><p>Mock 用于链路验证；HTTP Adapter 调用企业 AI 接口。</p></div><Badge tone={enabled ? 'green' : 'amber'}>{enabled ? '已启用' : '已关闭'}</Badge></header><article className="setting-card"><div className="form-stack"><div className="form-grid"><label>Adapter<select value={adapter} onChange={(e) => setAdapter(e.target.value as 'mock' | 'http')}><option value="mock">Mock</option><option value="http">HTTP</option></select></label><label className="checkbox-line"><input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />启用 AI 调用</label></div>{adapter === 'http' ? <><label>HTTP API URL<input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://ai.example.com/reply" /></label><label>Bearer Token<input type="password" value={token} onChange={(e) => setToken(e.target.value)} placeholder={adapterQuery.data?.token_configured ? '已配置，留空保持不变' : '输入 Token'} /></label><div className="form-grid"><label>超时秒数<input type="number" min={2} max={120} value={timeout} onChange={(e) => setTimeoutValue(Number(e.target.value))} /></label><label>连续失败转人工<input type="number" min={1} max={20} value={threshold} onChange={(e) => setThreshold(Number(e.target.value))} /></label></div></> : <><label>触发文本<input value={trigger} onChange={(event) => setTrigger(event.target.value)} /></label><label>回复文本<textarea rows={4} value={reply} onChange={(event) => setReply(event.target.value)} /></label></>}</div><div className="setting-actions"><button className="primary-button" onClick={() => adapter === 'mock' ? Promise.all([save.mutateAsync(), saveAdapter.mutateAsync()]) : saveAdapter.mutate()} disabled={save.isPending || saveAdapter.isPending || (adapter === 'http' && !url)}><Save size={15} />保存 AI 配置</button></div></article></>;
}

function HistorySettings({ notify }: { notify: (value: string) => void }) {
  const query = useQuery({ queryKey: ['history-sync'], queryFn: () => api<{ status: string; phase: string; current_page?: number; total_items: number; completed_items: number; failed_items: number; updated_at?: string }>('/settings/history-sync/status'), refetchInterval: 3000 });
  const start = useMutation({ mutationFn: () => api('/settings/history-sync', { method: 'POST' }), onSuccess: () => { query.refetch(); notify('历史回填任务已启动'); } });
  const pause = useMutation({ mutationFn: () => api('/settings/history-sync/pause', { method: 'POST' }), onSuccess: () => { query.refetch(); notify('历史回填任务已暂停'); } });
  const data = query.data; const progress = data?.total_items ? Math.min(100, Math.round(data.completed_items / data.total_items * 100)) : 0;
  return <><header className="settings-section-head"><div><h2>Chatwoot 全量历史</h2><p>后台分页回填全部会话和消息；历史数据不会触发 AI 或 SOP。</p></div><Badge tone={data?.status === 'completed' ? 'green' : data?.status === 'failed' ? 'red' : 'blue'}>{data?.status ?? '未开始'}</Badge></header><article className="setting-card"><div className="sync-progress"><div><strong>{data?.completed_items ?? 0} / {data?.total_items ?? 0}</strong><span>{progress}% · {data?.phase ?? 'idle'} · 第 {data?.current_page ?? 0} 页</span></div><div className="progress-track"><span style={{ width: `${progress}%` }} /></div><p>失败记录：{data?.failed_items ?? 0} · 最近更新：{data?.updated_at ? new Date(data.updated_at).toLocaleString() : '暂无'}</p></div><div className="setting-actions"><button className="primary-button" onClick={() => start.mutate()} disabled={start.isPending || ['pending', 'running'].includes(data?.status ?? '')}><Database size={15} />{data?.status === 'paused' ? '继续回填' : data?.status === 'failed' ? '重试回填' : '启动全量回填'}</button>{['pending', 'running'].includes(data?.status ?? '') ? <button className="secondary-button" onClick={() => pause.mutate()} disabled={pause.isPending}>暂停</button> : null}</div></article></>;
}

function MappingSettings({ notify }: { notify: (value: string) => void }) {
  const query = useQuery({ queryKey: ['label-mappings'], queryFn: () => api<Record<string, string[] | string>>('/settings/label-mappings') });
  const [form, setForm] = useState({ handoff_labels: '', contact_block_labels: '', lead_labels: '', conversion_labels: '', stage_labels: '', sop_whitelist_label: 'SOP测试白名单' });
  useEffect(() => { if (query.data) setForm({ handoff_labels: (query.data.handoff_labels as string[]).join(','), contact_block_labels: (query.data.contact_block_labels as string[]).join(','), lead_labels: (query.data.lead_labels as string[]).join(','), conversion_labels: (query.data.conversion_labels as string[]).join(','), stage_labels: (query.data.stage_labels as string[]).join(','), sop_whitelist_label: query.data.sop_whitelist_label as string }); }, [query.data]);
  const save = useMutation({ mutationFn: () => api('/settings/label-mappings', { method: 'PATCH', body: JSON.stringify({ ...Object.fromEntries(Object.entries(form).filter(([key]) => key !== 'sop_whitelist_label').map(([key, value]) => [key, value.split(',').map((x) => x.trim()).filter(Boolean)])), sop_whitelist_label: form.sop_whitelist_label }) }), onSuccess: () => notify('标签映射已保存') });
  return <><header className="settings-section-head"><div><h2>Chatwoot 标签映射</h2><p>使用逗号分隔；Chatwoot 修改标签后会通过 Webhook 实时生效。</p></div></header><article className="setting-card"><div className="form-stack"><label>人工接管标签<input value={form.handoff_labels} onChange={(e) => setForm({ ...form, handoff_labels: e.target.value })} /></label><label>联系人阻断标签<input value={form.contact_block_labels} onChange={(e) => setForm({ ...form, contact_block_labels: e.target.value })} /></label><div className="form-grid"><label>留资标签<input value={form.lead_labels} onChange={(e) => setForm({ ...form, lead_labels: e.target.value })} /></label><label>成交标签<input value={form.conversion_labels} onChange={(e) => setForm({ ...form, conversion_labels: e.target.value })} /></label></div><label>旅程阶段标签<input value={form.stage_labels} onChange={(e) => setForm({ ...form, stage_labels: e.target.value })} /></label><label>SOP 实发白名单标签<input value={form.sop_whitelist_label} onChange={(e) => setForm({ ...form, sop_whitelist_label: e.target.value })} /></label></div><div className="setting-actions"><button className="primary-button" onClick={() => save.mutate()}><Save size={15} />保存映射</button></div></article></>;
}

interface PlatformUser { id: number; email: string; display_name: string; role: string; active: boolean; chatwoot_agent_id?: number; inbox_binding_ids: number[]; }
function UserSettings({ notify }: { notify: (value: string) => void }) {
  const qc = useQueryClient(); const users = useQuery({ queryKey: ['users'], queryFn: () => api<{ items: PlatformUser[] }>('/users') }); const resources = useQuery({ queryKey: ['resources'], queryFn: () => api<{ agents: { id: number; name: string; email?: string }[] }>('/settings/resources') }); const inboxes = useQuery({ queryKey: ['inboxes'], queryFn: () => api<Inbox[]>('/settings/inboxes') });
  const [email, setEmail] = useState(''); const [name, setName] = useState(''); const [role, setRole] = useState('agent'); const [agentId, setAgentId] = useState<number | null>(null); const [scopeIds, setScopeIds] = useState<number[]>([]); const [temporary, setTemporary] = useState('');
  const create = useMutation({ mutationFn: () => api<PlatformUser & { temporary_password: string }>('/users', { method: 'POST', body: JSON.stringify({ email, display_name: name, role, chatwoot_agent_id: agentId, inbox_binding_ids: scopeIds }) }), onSuccess: (data) => { setTemporary(data.temporary_password); setEmail(''); setName(''); qc.invalidateQueries({ queryKey: ['users'] }); notify('用户已创建'); } });
  const patch = useMutation({ mutationFn: ({ id, body }: { id: number; body: object }) => api(`/users/${id}`, { method: 'PATCH', body: JSON.stringify(body) }), onSuccess: () => qc.invalidateQueries({ queryKey: ['users'] }) });
  return <><header className="settings-section-head"><div><h2>平台用户与 Chatwoot 绑定</h2><p>Agent 一对一绑定 Chatwoot 客服，并按 Inbox 限制数据范围。</p></div></header><article className="setting-card"><div className="form-stack"><div className="form-grid"><label>姓名<input value={name} onChange={(e) => setName(e.target.value)} /></label><label>邮箱<input type="email" value={email} onChange={(e) => setEmail(e.target.value)} /></label><label>角色<select value={role} onChange={(e) => setRole(e.target.value)}><option value="agent">Agent</option><option value="supervisor">Supervisor</option><option value="admin">Admin</option></select></label><label>Chatwoot Agent<select value={agentId ?? ''} onChange={(e) => setAgentId(Number(e.target.value) || null)}><option value="">不绑定</option>{resources.data?.agents.map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}</select></label></div><fieldset><legend>Inbox 数据范围</legend><div className="checkbox-grid">{inboxes.data?.map((x) => <label key={x.id}><input type="checkbox" checked={scopeIds.includes(x.id)} onChange={() => setScopeIds((current) => current.includes(x.id) ? current.filter((id) => id !== x.id) : [...current, x.id])} />{x.name}</label>)}</div></fieldset>{temporary ? <div className="info-box"><KeyRound size={16} /><span>一次性临时密码：<code>{temporary}</code></span></div> : null}</div><div className="setting-actions"><button className="primary-button" onClick={() => create.mutate()} disabled={!name || !email || create.isPending}><Users size={15} />创建用户</button></div></article><article className="panel"><div className="responsive-table"><table><thead><tr><th>用户</th><th>角色</th><th>Chatwoot Agent</th><th>Inbox 数</th><th>状态</th></tr></thead><tbody>{users.data?.items.map((x) => <tr key={x.id}><td><strong>{x.display_name}</strong><span className="table-sub">{x.email}</span></td><td>{x.role}</td><td>{resources.data?.agents.find((a) => a.id === x.chatwoot_agent_id)?.name ?? '未绑定'}</td><td>{x.inbox_binding_ids.length}</td><td><button className={`toggle ${x.active ? 'is-on' : ''}`} onClick={() => patch.mutate({ id: x.id, body: { active: !x.active } })}><span /></button></td></tr>)}</tbody></table></div></article></>;
}

function NotificationSettings({ notify }: { notify: (value: string) => void }) {
  type Config = { enabled: boolean; channel: 'webhook' | 'chatwoot'; agent_id?: number; bot_id?: number; url?: string; event_types: string[]; secret_configured: boolean };
  const query = useQuery({ queryKey: ['notification-settings'], queryFn: () => api<Config>('/settings/notifications') });
  const agents = useQuery({ queryKey: ['resources'], queryFn: () => api<{ agents: { id: number; name: string }[] }>('/settings/resources') });
  const [enabled, setEnabled] = useState(false);
  const [channel, setChannel] = useState<'webhook' | 'chatwoot'>('webhook');
  const [agentId, setAgentId] = useState('');
  const [botId, setBotId] = useState('');
  const [url, setUrl] = useState('');
  const [secret, setSecret] = useState('');
  useEffect(() => { if (query.data) { const data = query.data; setEnabled(data.enabled); setChannel(data.channel ?? 'webhook'); setAgentId(String(data.agent_id ?? '')); setBotId(String(data.bot_id ?? '')); setUrl(data.url ?? ''); } }, [query.data]);
  const save = useMutation({
    mutationFn: () => api('/settings/notifications', { method: 'PATCH', body: JSON.stringify({ enabled, channel, agent_id: Number(agentId) || null, bot_id: Number(botId) || null, url: url || null, secret: secret || null, event_types: query.data?.event_types ?? ['handoff.created', 'handoff.overdue'] }) }),
    onSuccess: () => { setSecret(''); query.refetch(); notify('顾问通知已保存'); },
    onError: (error: Error) => notify(error.message),
  });
  const invalid = enabled && (channel === 'chatwoot' ? !agentId || !Number.isInteger(Number(botId)) || Number(botId) <= 0 : !url);
  return <><header className="settings-section-head"><div><h2>顾问通知</h2><p>转人工和处理超时后提醒顾问；站内通知始终保留。</p></div><Badge tone={enabled ? 'green' : 'neutral'}>{enabled ? '已启用' : '已关闭'}</Badge></header>
    <article className="setting-card"><div className="form-stack">
      <div className="global-toggle-row"><strong>启用顾问通知</strong><Toggle checked={enabled} onChange={setEnabled} label="启用顾问通知" /></div>
      <label>提醒方式<select value={channel} onChange={(e) => setChannel(e.target.value as Config['channel'])}><option value="chatwoot">Chatwoot 手机提醒</option><option value="webhook">外部通知 Webhook</option></select></label>
      {channel === 'chatwoot' ? <>
        <div className="info-box"><Bell size={16} /><span>系统会在会话中发一条仅团队可见的备注并提及顾问。请在 Chatwoot 手机端开启提及通知；Apple Watch 接收情况取决于手机和手表的通知设置。</span></div>
        <label>接收顾问<select value={agentId} onChange={(e) => setAgentId(e.target.value)}><option value="">请选择顾问</option>{agents.data?.agents.map((agent) => <option key={agent.id} value={agent.id}>{agent.name}</option>)}</select></label>
        <label>通知机器人 ID<input type="number" min="1" step="1" value={botId} onChange={(e) => setBotId(e.target.value)} /></label>
        <div className="info-box"><ShieldCheck size={16} /><span>使用已配置的通知机器人，避免顾问账号提及自己而收不到提醒。暂停客户消息发送后，内部提醒仍可发送。</span></div>
      </> : <><label>Webhook URL<input value={url} onChange={(e) => setUrl(e.target.value)} /></label><label>HMAC Secret<input type="password" value={secret} onChange={(e) => setSecret(e.target.value)} placeholder={query.data?.secret_configured ? '已配置，留空保持不变' : '至少 8 位'} /></label></>}
      {query.isError ? <p role="alert">通知设置读取失败，请刷新后重试。</p> : null}
    </div><div className="setting-actions"><button className="primary-button" onClick={() => save.mutate()} disabled={invalid || save.isPending || !query.data}><Save size={15} />保存通知</button></div></article></>;
}

function AuditLogs() {
  const query = useQuery({ queryKey: ['audit-logs'], queryFn: () => api<{ items: { id: number; action: string; resource_type: string; resource_id?: string; user_id?: number; created_at: string }[] }>('/audit-logs') });
  return <><header className="settings-section-head"><div><h2>审计日志</h2><p>记录权限、配置、人工接管和 SOP 关键操作。</p></div></header><article className="panel">{query.isLoading ? <EmptyState type="loading" title="正在读取日志" description="" /> : <div className="responsive-table"><table><thead><tr><th>时间</th><th>操作</th><th>资源</th><th>用户 ID</th></tr></thead><tbody>{query.data?.items.map((x) => <tr key={x.id}><td>{new Date(x.created_at).toLocaleString()}</td><td><code>{x.action}</code></td><td>{x.resource_type} {x.resource_id ? `#${x.resource_id}` : ''}</td><td>{x.user_id ?? '系统'}</td></tr>)}</tbody></table></div>}</article></>;
}
