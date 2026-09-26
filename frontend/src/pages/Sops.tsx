import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Activity, CalendarClock, Copy, Edit3, Plus, Search, ShieldCheck, Workflow, Pause, ListChecks, Play, RotateCcw, ChevronLeft, ChevronRight, Upload } from 'lucide-react';
import { api } from '../api';
import { useAuth } from '../auth';
import SopReview from './SopReview';
import { Badge, EmptyState, Modal, PageHeader } from '../components';
import SopEditorDrawer from './SopEditorDrawer';
import { emptyForm, emptyNode, normalizeNode, timingLabel } from './sop-types';
import { statusLabel } from './Automation';
import type { NodeItem, Sop, SopForm } from './sop-types';

interface Candidate { id: number; name: string; inbox: string; round_number: number; round_status: string | null }
interface SopOptions { live_test_enabled: boolean; live_test_conversation_ids: number[] }
const splitLabels = (value: string) => [...new Set(value.split(/[,，]/).map(item => item.trim()).filter(Boolean))];
const triggerLabel = (value: string) => ({ manual: '手工入组', label: '新增会话标签', stage: '新增会话标签', first_message: '首次客户消息' } as Record<string, string>)[value] ?? value;

export default function Sops({ notify }: { notify: (message: string) => void }) {
  const qc = useQueryClient();
  const { user } = useAuth();
  const [reviewId, setReviewId] = useState<number | null>(null);
  const [query, setQuery] = useState('');
  const [status, setStatus] = useState('');
  const [editing, setEditing] = useState<Sop | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [enroll, setEnroll] = useState<Sop | null>(null);
  const [reenroll, setReenroll] = useState(false);
  const [requestKey, setRequestKey] = useState('');
  const [environment, setEnvironment] = useState('shadow');
  const [candidatePage, setCandidatePage] = useState(1);
  const [candidateSearch, setCandidateSearch] = useState('');
  const [selectedConversations, setSelectedConversations] = useState<number[]>([]);
  const [liveConfirmed, setLiveConfirmed] = useState(false);
  const [allowRepeatDelivery, setAllowRepeatDelivery] = useState(false);
  const [form, setForm] = useState<SopForm>(emptyForm);
  const sops = useQuery({ queryKey: ['sops', status, query], queryFn: () => api<{ items: Sop[] }>(`/sops?status=${status}&q=${encodeURIComponent(query)}`), refetchInterval: 10000 });
  const options = useQuery({ queryKey: ['sop-options'], queryFn: () => api<SopOptions>('/sops/options') });
  const candidates = useQuery({ queryKey: ['sop-candidates', enroll?.id, environment, candidatePage, candidateSearch], queryFn: () => api<{ items: Candidate[]; total: number }>(`/sops/${enroll!.id}/enrollment-candidates?page=${candidatePage}&q=${encodeURIComponent(candidateSearch)}&environment=${environment}`), enabled: Boolean(enroll) });
  const mutate = useMutation({
    mutationFn: ({ path, method = 'POST', body }: { path: string; method?: string; body?: object }) => api(path, { method, body: body ? JSON.stringify(body) : undefined }),
    onSuccess: (result) => { qc.invalidateQueries({ queryKey: ['sops'] }); qc.invalidateQueries({ queryKey: ['sop-candidates'] }); notify((result as { outbound?: boolean })?.outbound ? '真实 SOP 测试已入组，将按发布时间执行' : 'SOP 已更新'); },
  });
  useEffect(() => {
    if (!editing) return;
    setForm({
      route_variant: editing.route_variant ?? '', test_conversation_ids: editing.test_conversation_ids ?? [],
      time_anchor: editing.nodes.some(node => node.basis === 'customer_added') ? 'customer_added' : 'enrollment',
      inbox_ids: editing.inbox_ids, name: editing.name, description: editing.description,
      trigger_type: editing.trigger_type === 'stage' ? 'label' : editing.trigger_type,
      trigger_labels: editing.trigger_labels.join(','), exit_labels: editing.exit_labels.join(','),
      stop_on_incoming: true, frequency_hours: editing.frequency_hours, dry_run: true, live_enabled: false,
      nodes: editing.nodes.length ? editing.nodes.map(normalizeNode) : [emptyNode()],
    });
  }, [editing]);
  async function save() {
    const body = { name: form.name.trim(), description: form.description, trigger_type: form.trigger_type,
      route_variant: form.route_variant, test_conversation_ids: form.test_conversation_ids,
      trigger_labels: form.trigger_type === 'label' ? splitLabels(form.trigger_labels) : [], inbox_ids: form.inbox_ids,
      expected_version: editing?.version, nodes: form.nodes, exit_labels: splitLabels(form.exit_labels),
      stop_on_incoming: true, frequency_hours: form.frequency_hours, dry_run: true, live_enabled: false };
    await mutate.mutateAsync({ path: editing ? `/sops/${editing.id}` : '/sops', method: editing ? 'PATCH' : 'POST', body });
    setEditing(null); setCreateOpen(false);
  }
  function openEnrollment(item: Sop, again: boolean) {
    mutate.reset();
    setEnroll(item); setReenroll(again); setSelectedConversations([]); setLiveConfirmed(false); setAllowRepeatDelivery(false); setCandidatePage(1); setCandidateSearch(''); setRequestKey(crypto.randomUUID());
  }
  const updateNode = (index: number, patch: Partial<NodeItem>) => setForm(current => ({ ...current, nodes: current.nodes.map((node, i) => i === index ? { ...node, ...patch } : node) }));
  const summary = sops.data?.items ?? [];
  const admin = user?.roles.includes('admin');

  return <div className="page-content sop-page">
    <PageHeader eyebrow="STANDARD OPERATING PROCEDURES" title="SOP 定时触达" description="" actions={<button className="primary-button" onClick={() => { setForm(emptyForm()); setCreateOpen(true); }}><Plus size={17} />新建 SOP</button>} />
    <section className="sop-summary-grid">
      <article><Workflow size={18} /><div><strong>{summary.filter(x => x.status === 'running').length}</strong><span>运行中策略</span></div></article>
      <article><CalendarClock size={18} /><div><strong>{summary.reduce((n, x) => n + (x.rehearsal_enrolled ?? 0), 0)}</strong><span>演练入组轮次</span></div></article>
      <article><Activity size={18} /><div><strong>{summary.reduce((n, x) => n + (x.rehearsal_sent ?? 0), 0)}</strong><span>模拟完成内容组</span></div></article>
      <article><ShieldCheck size={18} /><div><strong>{summary.reduce((n, x) => n + (x.rehearsal_blocked ?? 0), 0)}</strong><span>拦截 / 取消内容组</span></div></article>
    </section>
    <section className="sop-toolbar"><div className="toolbar-search"><Search size={16} /><input value={query} onChange={e => setQuery(e.target.value)} placeholder="搜索 SOP" /></div><select className="control" aria-label="策略状态" value={status} onChange={e => setStatus(e.target.value)}><option value="">全部状态</option><option value="running">运行中</option><option value="paused">已暂停</option><option value="draft">草稿</option></select><Badge tone={options.data?.live_test_enabled ? 'amber' : 'blue'}>{options.data?.live_test_enabled ? `真实测试已启用 · 仅会话 ${options.data.live_test_conversation_ids.map(id => `#${id}`).join('、')}` : '本地演练 · 真实发送关闭'}</Badge></section>
    {sops.error && <div className="automation-error">{sops.error.message}</div>}
    {mutate.error && !enroll && !createOpen && !editing && <div className="automation-error" role="alert">{mutate.error.message}</div>}
    {reviewId && <SopReview id={reviewId} onClose={() => setReviewId(null)} />}
    <section className="panel sop-list"><header className="sop-list-head"><span>SOP</span><span>状态</span><span>发布版本</span><span>内容组</span><span>演练执行</span><span>更新</span><span /></header>
      {sops.isLoading ? <EmptyState type="loading" title="正在读取 SOP" description="" /> : !summary.length ? <EmptyState title="暂无 SOP" description="" /> : summary.map(item => <article className="sop-row" key={item.id}>
        <div className="sop-name"><span className="sop-icon"><Workflow size={18} /></span><div><strong>{item.name}</strong>{item.description && <p>{item.description}</p>}<Badge>{triggerLabel(item.trigger_type)}</Badge></div></div>
        <div><Badge tone={item.status === 'running' ? 'green' : 'neutral'}>{statusLabel(item.status)}</Badge></div>
        <div className="sop-version-cell"><Badge>{item.published_version ? `已发布 v${item.published_version}` : '未发布'}</Badge>{item.has_unpublished_changes && <><span className="table-sub sop-unpublished-warning">草稿 v{item.version} 未发布</span><span className="table-sub">新入组仍执行已发布版本</span></>}{item.published_nodes?.length ? <span className="table-sub" title={item.published_nodes.map(timingLabel).join(' · ')}>时间：{item.published_nodes.map(timingLabel).join(' · ')}</span> : null}</div>
        <div><strong>{item.nodes.length}</strong><span className="table-sub">个节点</span></div>
        <div className="sop-performance"><strong>{item.rehearsal_sent ?? 0}</strong><span>完成 · 拦截 {item.rehearsal_blocked ?? 0}</span></div>
        <div>{new Date(item.updated_at).toLocaleString()}</div>
        <div className="row-actions">
          <button title="编辑草稿" disabled={mutate.isPending} onClick={() => setEditing(item)}><Edit3 size={16} /></button>
          <button title="版本与执行明细" onClick={() => setReviewId(item.id)}><ListChecks size={16} /></button>
          <button title="复制" disabled={mutate.isPending} onClick={() => mutate.mutate({ path: '/sops', body: { ...item, name: `${item.name} 副本` } })}><Copy size={16} /></button>
          <button title="首次入组" disabled={item.status !== 'running' || mutate.isPending} onClick={() => openEnrollment(item, false)}><Plus size={16} /></button>
          <button title="重新入组" disabled={item.status !== 'running' || mutate.isPending} onClick={() => openEnrollment(item, true)}><RotateCcw size={16} /></button>
          {admin && (!item.published_version || item.has_unpublished_changes) && <button title="发布草稿" disabled={mutate.isPending} onClick={() => mutate.mutate({ path: `/sops/${item.id}/publish` })}><Upload size={16} /></button>}
          {admin && item.published_version && <button title={item.status === 'running' ? '暂停策略' : '恢复策略'} disabled={mutate.isPending} onClick={() => mutate.mutate({ path: `/sops/${item.id}/${item.status === 'running' ? 'pause' : 'resume'}` })}>{item.status === 'running' ? <Pause size={16} /> : <Play size={16} />}</button>}
        </div>
      </article>)}
    </section>
    <SopEditorDrawer open={createOpen || Boolean(editing)} editing={editing} form={form} setForm={setForm} updateNode={updateNode} save={save} saving={mutate.isPending} notify={notify} onClose={() => { setEditing(null); setCreateOpen(false); }} />
    <Modal open={Boolean(enroll)} title={reenroll ? '重新入组' : '首次入组'} description={environment === 'live_test' ? `真实测试将锁定已发布 v${enroll?.published_version ?? '-'}，已入组后修改草稿不会改变本轮时间。` : '仅创建本地演练，不会向客户发送。'} onClose={() => !mutate.isPending && setEnroll(null)} footer={<><button className="secondary-button" disabled={mutate.isPending} onClick={() => setEnroll(null)}>取消</button><button className="primary-button" disabled={!selectedConversations.length || mutate.isPending || (environment === 'live_test' && !liveConfirmed)} onClick={async () => {
      if (!enroll) return;
      try { await mutate.mutateAsync({ path: `/sops/${enroll.id}/enroll`, body: { conversation_ids: selectedConversations, reenroll, request_key: requestKey, environment, confirm_live_delivery: environment === 'live_test' && liveConfirmed, allow_repeat_delivery: environment === 'live_test' && allowRepeatDelivery } }); setEnroll(null); } catch { /* The mutation renders the server error. */ }
    }}>{mutate.isPending ? '处理中…' : environment === 'live_test' ? '确认真实入组' : reenroll ? '确认重新入组' : '确认入组'}</button></>}>
      <div className="sop-enroll-controls"><label>入组模式<select value={environment} onChange={e => { setEnvironment(e.target.value); setSelectedConversations([]); setLiveConfirmed(false); setAllowRepeatDelivery(false); setCandidatePage(1); }}><option value="shadow">Chatwoot 实时事件演练</option><option value="playground">隔离历史副本演练</option>{admin && options.data?.live_test_enabled && <option value="live_test">真实测试（仅白名单）</option>}</select></label><input aria-label="搜索入组客户" placeholder="客户名称或会话 ID" value={candidateSearch} onChange={e => { setCandidateSearch(e.target.value); setCandidatePage(1); }} /></div>
      {enroll?.has_unpublished_changes && <div className="sop-publish-warning" role="status"><strong>当前草稿 v{enroll.version} 尚未发布</strong><span>本次入组仍执行已发布 v{enroll.published_version ?? '-'}：{enroll.published_nodes?.map(timingLabel).join(' · ') || '暂无可执行节点'}。请先关闭弹窗并点击“发布草稿”，再重新打开入组。</span></div>}
      {environment === 'live_test' && <label className="sop-live-confirm"><input type="checkbox" checked={liveConfirmed} onChange={e => setLiveConfirmed(e.target.checked)} /><span><strong>我确认这会向真实 Facebook 测试账号发送</strong><small>发送前仍会检查 ai 标签、人工接管、客户新回复、24 小时窗口和素材版本；任何一项不满足都会拦截。</small></span></label>}
      {environment === 'live_test' && <label className="sop-repeat-confirm"><input type="checkbox" checked={allowRepeatDelivery} onChange={e => setAllowRepeatDelivery(e.target.checked)} /><span><strong>允许本轮重复发送已提供素材</strong><small>仅白名单测试账号可用；同时忽略本轮 24 小时测试频控，但不会绕过 Facebook 消息窗口、AI 标签或人工接管检查。</small></span></label>}
      {mutate.error && <p className="sop-field-error" role="alert">{mutate.error.message}</p>}
      {candidates.error ? <p role="alert">{candidates.error.message}</p> : candidates.isLoading ? <EmptyState type="loading" title="正在读取客户" description="" /> : !candidates.data?.items.length ? <EmptyState title="暂无匹配客户" description="" /> : <div className="selection-list">{candidates.data.items.map(item => <label key={item.id}><input type="checkbox" checked={selectedConversations.includes(item.id)} disabled={reenroll ? !item.round_status || ['active', 'attention_required'].includes(item.round_status) : Boolean(item.round_status)} onChange={e => setSelectedConversations(current => e.target.checked ? [...current, item.id] : current.filter(id => id !== item.id))} /><span><strong>{item.name}</strong><small>{item.inbox} · #{item.id} · {item.round_status ? `第 ${item.round_number} 轮 ${statusLabel(item.round_status)}` : '从未入组'}</small></span></label>)}</div>}
      <div className="sop-enroll-pagination"><button className="icon-button" title="上一页" disabled={candidatePage <= 1} onClick={() => setCandidatePage(x => x - 1)}><ChevronLeft size={16} /></button><span>第 {candidatePage} 页 · {candidates.data?.total ?? 0} 个会话</span><button className="icon-button" title="下一页" disabled={candidatePage * 25 >= (candidates.data?.total ?? 0)} onClick={() => setCandidatePage(x => x + 1)}><ChevronRight size={16} /></button></div>
    </Modal>
  </div>;
}
