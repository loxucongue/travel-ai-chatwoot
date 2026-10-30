import { useEffect, useRef, useState, type CSSProperties } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useSearchParams } from 'react-router-dom';
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { ArrowUpRight, RefreshCw, X } from 'lucide-react';
import { api } from '../api';
import { EmptyState, PageHeader } from '../components';
import { type BiReport, type BiCustomers, localDay, validRange, metricHelp, deliveryLabels, stateLabels } from './bi-types';
import './overview.css';

const colors = { new: '#164f7e', engaged: '#6a58a1', captured: '#148071', returned: '#d09138' };
const trendLabels = { new: '新增客户', engaged: '开口客户', captured: '取得联系方式', returned: '跟进后回复' };
const time = (s: string | null) => s ? new Date(s).toLocaleString('zh-CN', { timeZone: 'Asia/Taipei', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }) : '暂无记录';
const sourceLabels: Record<string, string> = { label_observed:'标签变化收到时间', current_label:'当前标签快照（不是添加时间）', chatwoot_created:'渠道创建记录', first_observed_message:'首次可见消息（历史不完整）', message:'实际消息', message_rule:'有效客户回复', business_reply:'开口消息记录', business_label:'业务标签', live_outbox:'正式发送记录', handoff_task:'转人工任务', handoff_evidence:'联系方式交接', consultation_evidence:'有效咨询记录', followup_reply:'跟进后的首次回复', live_plan:'图文跟进轮次' };

export default function Overview({ onNavigate }: { onNavigate: (page: string) => void }) {
  const [params, setParams] = useSearchParams();
  const start = params.get('start') || localDay(), end = params.get('end') || localDay();
  const mode = params.get('mode') === 'cohort' ? 'cohort' : 'period';
  const inbox = params.get('inbox_id') || '';
  const [draftStart, setDraftStart] = useState(start), [draftEnd, setDraftEnd] = useState(end);
  const [error, setError] = useState('');
  const [metric, setMetric] = useState<string | null>(null), [page, setPage] = useState(1);
  const closeRef = useRef<HTMLButtonElement>(null), drawerRef = useRef<HTMLElement>(null);
  const triggerRef = useRef<HTMLElement | null>(null);
  const query = new URLSearchParams({ start, end, mode, ...(inbox ? { inbox_id: inbox } : {}) }).toString();
  const report = useQuery({ queryKey: ['bi-dashboard', query], queryFn: () => api<BiReport>(`/bi/dashboard?${query}`), refetchInterval: 60000, retry: 1 });
  const details = useQuery({ queryKey: ['bi-customers', query, metric, page, report.dataUpdatedAt], queryFn: () => api<BiCustomers>(`/bi/customers?${query}&metric=${metric}&page=${page}`), enabled: !!metric, retry: 1 });
  const data = report.data;
  const stale = !data?.meta.refreshed_at || Date.now() - Date.parse(data.meta.refreshed_at) > 180000;
  useEffect(() => { setDraftStart(start); setDraftEnd(end); setMetric(null); setError(''); }, [start, end, mode, inbox]);
  useEffect(() => {
    if (!metric) return;
    const previous = document.activeElement as HTMLElement;
    closeRef.current?.focus();
    const oldOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    const listener = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setMetric(null);
      if (e.key === 'Tab') {
        const elements = [...(drawerRef.current?.querySelectorAll<HTMLElement>('button:not(:disabled), a[href]') || [])];
        const first = elements[0], last = elements.at(-1);
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last?.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first?.focus(); }
      }
    };
    document.addEventListener('keydown', listener);
    return () => { document.body.style.overflow = oldOverflow; document.removeEventListener('keydown', listener); (triggerRef.current || previous)?.focus(); };
  }, [metric]);
  function change(values: Record<string, string>) { const next = new URLSearchParams(query); for (const [k, v] of Object.entries(values)) v ? next.set(k, v) : next.delete(k); setParams(next); }
  function open(key: string) { triggerRef.current = document.activeElement as HTMLElement; setPage(1); setMetric(key); }
  function applyDates() { if (!validRange(draftStart, draftEnd)) { setError('请选择有效日期：结束不晚于今天，跨度不超过366天。'); return; } setError(''); change({ start: draftStart, end: draftEnd }); }
  const valueRow = (label: string, value: number) => <div className="bi-value-row"><span>{label}</span><strong>{value.toLocaleString()}</strong></div>;
  return <div className="page-content bi-page">
    <PageHeader eyebrow="CHINA2GO · OPERATIONS" title="AI 运营看板" description="从客户进入到私讯留资，看清接待进展与跟进效果。" actions={<button className="secondary-button" onClick={() => void report.refetch()} disabled={report.isFetching}><RefreshCw size={14} />{report.isFetching ? '刷新中' : '刷新数据'}</button>} />
    <section className="panel bi-filters" aria-label="数据筛选">
      <div className="bi-presets">{[[0, 0, '今天'], [-1, -1, '昨天'], [-6, 0, '近7天'], [-29, 0, '近30天']].map(([a, b, label]) => <button key={label} className={start === localDay(Number(a)) && end === localDay(Number(b)) ? 'active' : ''} onClick={() => change({ start: localDay(Number(a)), end: localDay(Number(b)) })}>{label}</button>)}</div>
      <label>开始日期<input type="date" value={draftStart} max={localDay()} onChange={e => setDraftStart(e.target.value)} /></label>
      <label>结束日期<input type="date" value={draftEnd} max={localDay()} onChange={e => setDraftEnd(e.target.value)} /></label>
      <button className="secondary-button" onClick={applyDates}>应用日期</button>
      <label>渠道<select value={inbox} onChange={e => change({ inbox_id: e.target.value })}><option value="">全部可见渠道</option>{data?.inboxes.map(i => <option key={i.id} value={i.id}>{i.name}</option>)}</select></label>
      <div className="bi-tabs" aria-label="统计视角"><button className={mode === 'period' ? 'active' : ''} aria-pressed={mode === 'period'} onClick={() => change({ mode: 'period' })}>期间发生</button><button className={mode === 'cohort' ? 'active' : ''} aria-pressed={mode === 'cohort'} onClick={() => change({ mode: 'cohort' })}>新客后续进展</button></div>
      {error && <div role="alert" className="bi-error">{error}</div>}
    </section>
    <div className="bi-context"><span>{mode === 'period' ? '按事件发生时间统计，包含新客与老客；各阶段分别按客户去重。' : '仅看所选日期首次进入的新客，追踪其截至当前的进展。'} · 时间统一为 UTC+8</span><span>数据更新：{time(data?.meta.refreshed_at || null)}</span></div>
    {report.isLoading ? <section className="panel"><EmptyState type="loading" title="正在读取运营数据" description="" /></section> : report.isError || !data ? <section className="panel"><EmptyState type="error" title="运营数据读取失败" description={report.error?.message || '请重试。'} /></section> : <>
      {(stale || data.meta.remaining > 0 || data.meta.covered < data.meta.conversations || data.meta.incomplete_history > 0 || data.meta.unlinked_customers > 0) && <div className="bi-notice" role="status">{stale ? '统计更新延迟，当前结果可能尚未包含最新消息。 ' : ''}{data.meta.covered < data.meta.conversations || data.meta.remaining > 0 ? `历史数据正在整理：已覆盖 ${data.meta.covered}/${data.meta.conversations} 条会话。 ` : ''}{data.meta.incomplete_history > 0 ? `${data.meta.incomplete_history} 位客户缺少原始创建时间，以首次可见客户消息计入新增。 ` : ''}{data.meta.unlinked_customers > 0 ? `${data.meta.unlinked_customers} 条会话未关联客户，暂按会话单独计数。` : ''}</div>}
      {data.meta.unknown_birth > 0 && <div className="bi-notice">另有 {data.meta.unknown_birth} 位历史客户无法确定进入日期，暂不纳入新增及新客后续进展统计；其有时间依据的业务事件仍正常统计。</div>}
      <div className="bi-context"><span>当前所选渠道已有 <button className="text-button" onClick={() => open('current_added')}>{data.meta.current_added} 位客户确认加私讯</button>（当前快照，不受日期限制）。其中 {data.meta.added_time_unknown} 位缺少添加时间，未计入下面按日期统计的确认数。</span></div>
      <section className="bi-cards" aria-label="核心指标">{data.metrics.map(m => <button key={m.key} className="bi-card" onClick={() => open(m.key)} title={metricHelp[m.key]}><span>{m.label}<ArrowUpRight size={14} /></span><strong>{m.value.toLocaleString()}</strong><small>{m.rate == null ? '位客户 · 点击查看明细' : `占这批新客 ${m.rate}% · 查看明细`}</small></button>)}</section>
      <section className="bi-grid">
        <article className="panel bi-panel"><h2>客户进展趋势</h2><p>{mode === 'period' ? '每天按客户去重，同一客户可能在不同日期出现。' : '仅展示这批新客在所选日期内的事件；上方卡片还包含日期之后的进展。'}</p><div className="bi-chart"><ResponsiveContainer width="100%" height="100%"><AreaChart data={data.trends} margin={{ top: 10, right: 8, left: -20, bottom: 0 }}><CartesianGrid stroke="#e8eef1" vertical={false} /><XAxis dataKey="day" tickFormatter={s => s.slice(5)} tickLine={false} axisLine={false} /><YAxis allowDecimals={false} tickLine={false} axisLine={false} /><Tooltip />{(Object.keys(colors) as (keyof typeof colors)[]).map(k => <Area key={k} type="monotone" dataKey={k} name={trendLabels[k]} stroke={colors[k]} fill={colors[k]} fillOpacity={k === 'new' ? .08 : 0} strokeWidth={2} />)}</AreaChart></ResponsiveContainer></div><div className="bi-legend">{Object.entries(colors).map(([k,c]) => <span key={k} style={{ '--dot':c } as CSSProperties}>{trendLabels[k as keyof typeof trendLabels]}</span>)}</div></article>
        <article className="panel bi-panel"><h2>AI 在做什么</h2><p>同一统计范围内的实际参与记录。</p>{valueRow('AI 接待、未见人工回复', data.ai_value.ai_only)}{valueRow('AI 与顾问共同接待', data.ai_value.mixed)}{valueRow('有 AI 参与且取得联系方式', data.ai_value.captured_with_ai)}{valueRow('发生过发送失败', data.ai_value.failed_customers)}<p>“未见人工回复”不代表问题已解决；留资也可能由顾问共同促成。失败数保留真实失败记录。</p></article>
      </section>
      <section className="bi-grid">
        <article className="panel bi-panel"><h2>客户走到了哪一步</h2><p>可以跳过阶段，各项不能相加，也不强制排成递减漏斗。</p><div className="bi-progress">{data.progress.map(m => <button key={m.key} onClick={() => open(m.key)}><span>{m.label}</span><strong>{m.value}</strong></button>)}</div></article>
        <article className="panel bi-panel"><h2>转人工情况</h2><p><button className="text-button" onClick={() => open('handoff')}>{data.handoffs.customers} 位客户</button> · {data.handoffs.tasks} 次任务；按原因分别去重。</p><div className="bi-bars">{data.handoffs.reasons.length ? data.handoffs.reasons.map(r => <div key={r.name}><span>{r.name}</span><i><b style={{ width:`${r.value / Math.max(1,...data.handoffs.reasons.map(x => x.value)) * 100}%` }} /></i><strong>{r.value}</strong></div>) : <p>所选范围没有转人工记录。</p>}</div><p>{data.handoffs.answered} 次交接之后已有顾问公开回复；平均首次回复 {data.handoffs.mean_reply_minutes == null ? '—' : `${data.handoffs.mean_reply_minutes} 分钟`}。分配和通知成功不算顾问已回复。</p></article>
      </section>
      <article className="panel bi-panel" style={{ marginBottom:18 }}><h2>沉默跟进有没有接回来</h2><p>同一轮图片和配文合并为一次跟进；普通跟进与顾问交回后的跟进分别记录。</p><div className="bi-mini"><button onClick={() => open('followup')}>跟进客户<strong>{data.followup.customers}</strong>{data.followup.rounds} 轮</button><button onClick={() => open('returned')}>跟进后重新回复<strong>{data.followup.returned}</strong>24小时内首次有效回复</button><button onClick={() => open('meaningful_return')}>继续咨询<strong>{data.followup.meaningful_return}</strong>排除单纯致谢、点赞</button><button onClick={() => open('followup_lead')}>回复后取得联系方式<strong>{data.followup.leads}</strong>跟进后24小时内</button></div>
        <div className="bi-pills">{Object.entries(data.followup.delivery).map(([k,v]) => <span key={k} className={['failed','partial'].includes(k) ? 'bi-danger' : ''}>{deliveryLabels[k] || '状态未知'} {v}轮</span>)}<span>普通跟进 {data.followup.modes.ordinary || 0}轮</span><span>顾问交回 {data.followup.modes.advisor || 0}轮</span></div>
        <p><strong>24小时回复率：{data.followup.response_rate == null ? '观察中／暂无成熟样本' : `${data.followup.response_rate}%`}</strong>（{data.followup.matured_replied}/{data.followup.matured_customers}位）；另有 {data.followup.observing} 位仍在观察。按所选发送范围内每位客户最后一次有效跟进计算，满24小时才进入分母。上方回复数按回复发生日期统计，因此与此分子可能不同。{data.followup.shared_rounds > 0 ? `其中 ${data.followup.shared_rounds} 轮回复前还有顾问参与，不单独归功于 AI。` : ''}</p>
      </article>
      <section className="panel bi-current"><div><h2>现在需要关注</h2><p>当前队列快照 · 随渠道筛选，不受日期限制</p></div><div className="bi-current-items"><button onClick={() => open('pending')}><strong>{data.handoffs.pending}</strong>待人工回复</button><button onClick={() => open('overdue')}><strong>{data.handoffs.overdue}</strong>已超过交接时限</button><div><strong>{data.followup.queue.scheduled || 0}</strong>待执行跟进会话</div><div><strong>{data.followup.queue.advisor_waiting || 0}</strong>顾问交回等待中</div><button className="secondary-button" onClick={() => onNavigate('handoff')}>处理交接 <ArrowUpRight size={14} /></button></div><p>待回复任务：{data.handoffs.unassigned} 次未分配，{data.handoffs.assigned_unanswered} 次已分配未见回复。</p></section>
      <details className="panel bi-definitions"><summary>统计口径与数据边界</summary><dl>{data.metrics.map(m => <div key={m.key} style={{ display:'contents' }}><dt>{m.label}</dt><dd>{metricHelp[m.key]}</dd></div>)}</dl><p>历史记录只能还原已有证据，无法补造缺失事件。跨渠道只有关联到同一客户账号才合并，不通过姓名猜测。业务标签反映人员确认记录，不等于订单或收款。所有数据只包含当前账号可见渠道；演练运行不计入正式发送。</p></details>
    </>}
    {metric && <div className="bi-overlay" onMouseDown={e => { if (e.target === e.currentTarget) setMetric(null); }}><section className="bi-drawer" role="dialog" aria-modal="true" aria-labelledby="bi-detail-title" ref={drawerRef}><header><div><h2 id="bi-detail-title">{details.data?.label || data?.metrics.find(m => m.key === metric)?.label || '客户明细'}</h2><p>{['pending','overdue','current_added'].includes(metric) ? '当前状态快照（不受日期限制）' : `${start} 至 ${end} · ${mode === 'period' ? '期间发生' : '新客后续进展'}`} · {details.data?.total ?? '…'} 位客户</p></div><button ref={closeRef} aria-label="关闭明细" onClick={() => setMetric(null)}><X size={18} /></button></header><div className="bi-detail-list">{details.isLoading ? <EmptyState type="loading" title="正在读取明细" description="" /> : details.isError ? <EmptyState type="error" title="明细读取失败" description={details.error.message} /> : !details.data?.items.length ? <p>所选范围暂无符合条件的客户。</p> : details.data.items.map(c => <article className="bi-person" key={c.customer_key}><div className="bi-person-title"><strong>{c.name}</strong><a href={`#/conversations?conversation=${c.conversation_id}`}>打开会话 #{c.conversation_id} ↗</a></div><p>{stateLabels[c.state] || '其他接待状态'} · 顾问：{c.advisor || '未分配'}<br />{metric === 'current_added' ? '快照查询时间：' : '最近相关记录：'}{time(c.at)}{c.next_followup_at && <> · 下一跟进检查：{time(c.next_followup_at)}</>}</p><ul>{c.evidence.map((e,i) => <li key={i}>{time(e.at)} · {sourceLabels[e.source] || '业务事件'}{e.message_id ? ` · 消息 #${e.message_id}` : ''}{e.task_id ? ` · 交接 #${e.task_id}` : ''}{e.status && deliveryLabels[e.status] ? ` · ${deliveryLabels[e.status]}` : ''}{e.label ? ` · ${e.label}` : ''}{e.shared ? ' · 顾问共同参与' : ''}</li>)}</ul></article>)}</div><footer><button disabled={page === 1 || details.isFetching} onClick={() => setPage(p => p-1)}>上一页</button><span>第 {page} 页 / {Math.max(1, Math.ceil((details.data?.total || 0)/20))} 页</span><button disabled={page*20 >= (details.data?.total || 0) || details.isFetching} onClick={() => setPage(p => p+1)}>下一页</button></footer></section></div>}
  </div>;
}
