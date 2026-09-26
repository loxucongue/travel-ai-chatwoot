import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { AlertTriangle, ArrowRight, Bot, ContactRound, MessageSquareText, UserRoundCheck } from 'lucide-react';
import { api } from '../api';
import { EmptyState, MetricCard, PageHeader } from '../components';

interface OverviewData { conversations: number; incoming_messages: number; outgoing_messages: number; ai_only: number; mixed: number; human_only: number; ai_handled: number; handoffs: number; handoff_pending: number; handoff_overdue: number; leads: number; conversions: number; ai_errors: number; inferred_history: number; }
interface Trend { day: string; incoming: number; ai: number; human: number; }
interface FunnelItem { name: string; value: number; }

const percent = (value: number, total: number) => total ? `${(value / total * 100).toFixed(1)}%` : '0.0%';

export default function Overview({ onNavigate }: { onNavigate: (page: string) => void }) {
  const [days, setDays] = useState(7);
  const overview = useQuery({ queryKey: ['bi-overview', days], queryFn: () => api<OverviewData>(`/bi/overview?days=${days}`), refetchInterval: 30000 });
  const trends = useQuery({ queryKey: ['bi-trends', days], queryFn: () => api<{ items: Trend[] }>(`/bi/trends?days=${days}`) });
  const funnel = useQuery({ queryKey: ['bi-funnel', days], queryFn: () => api<{ items: FunnelItem[] }>(`/bi/funnel?days=${days}`) });
  const data = overview.data;
  return <div className="page-content">
    <PageHeader eyebrow="OPERATIONS COMMAND" title="运营总览" description="基于本地事件镜像统计 AI、人工接管与上线后的留资转化。" actions={<div className="segmented">{[[1, '今天'], [7, '7天'], [30, '30天']].map(([value, label]) => <button key={value} className={days === value ? 'active' : ''} onClick={() => setDays(Number(value))}>{label}</button>)}</div>} />
    {overview.isLoading ? <section className="panel"><EmptyState type="loading" title="正在计算指标" description="" /></section> : overview.isError || !data ? <section className="panel"><EmptyState type="error" title="无法读取运营数据" description="请确认 API 与数据库状态。" /></section> : <>
      <section className="metric-grid">
        <MetricCard label="有效会话" value={String(data.conversations)} detail={`${data.incoming_messages} 条入站消息`} tone="blue" icon={<MessageSquareText size={19} />} />
        <MetricCard label="AI 参与率" value={percent(data.ai_handled, data.conversations)} detail={`${data.ai_only} AI 独立 · ${data.mixed} 混合`} tone="green" icon={<Bot size={19} />} />
        <MetricCard label="待人工接管" value={String(data.handoff_pending)} detail={`${data.handoff_overdue} 个超过 SLA`} tone="amber" icon={<UserRoundCheck size={19} />} />
        <MetricCard label="上线后留资率" value={percent(data.leads, data.conversations)} detail={`${data.leads} 留资 · ${data.conversions} 成交`} tone="blue" icon={<ContactRound size={19} />} />
        <MetricCard label="AI 异常" value={String(data.ai_errors)} detail={`${data.inferred_history} 条历史人工推断`} tone={data.ai_errors ? 'red' : 'neutral'} icon={<AlertTriangle size={19} />} />
      </section>
      <section className="dashboard-grid overview-dashboard">
        <article className="panel dashboard-main-chart"><header className="panel-header"><div><h2>消息处理趋势</h2><p>AI 消息仅统计可关联的本平台发送记录</p></div><div className="legend"><span><i className="legend-incoming" />客户消息</span><span><i className="legend-ai" />AI 回复</span><span><i className="legend-human" />人工回复</span></div></header><div className="chart-wrap"><ResponsiveContainer width="100%" height="100%"><AreaChart data={trends.data?.items ?? []} margin={{ top: 12, right: 10, left: -18 }}><defs><linearGradient id="incomingFill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="#0f4c81" stopOpacity={0.24} /><stop offset="100%" stopColor="#0f4c81" stopOpacity={0.02} /></linearGradient></defs><CartesianGrid stroke="#e5e9ef" vertical={false} /><XAxis dataKey="day" axisLine={false} tickLine={false} /><YAxis axisLine={false} tickLine={false} /><Tooltip /><Area type="monotone" dataKey="incoming" name="客户消息" stroke="#0f4c81" strokeWidth={2} fill="url(#incomingFill)" /><Area type="monotone" dataKey="ai" name="AI 回复" stroke="#7c3aed" strokeWidth={2} fill="transparent" /><Area type="monotone" dataKey="human" name="人工回复" stroke="#006a60" strokeWidth={2} fill="transparent" /></AreaChart></ResponsiveContainer></div></article>
        <article className="panel health-panel"><header className="panel-header"><div><h2>处理结构</h2><p>按会话去重</p></div></header><div className="health-list"><div className="health-row"><i className="status-dot status-dot-purple" /><div><strong>AI 独立处理</strong><span>没有人工出站</span></div><em>{data.ai_only}</em></div><div className="health-row"><i className="status-dot status-dot-blue" /><div><strong>AI + 人工</strong><span>混合处理会话</span></div><em>{data.mixed}</em></div><div className="health-row"><i className="status-dot status-dot-amber" /><div><strong>纯人工</strong><span>包含历史推断人工</span></div><em>{data.human_only}</em></div></div><button className="secondary-button full-width" onClick={() => onNavigate('handoff')}>查看人工接管队列<ArrowRight size={15} /></button></article>
        <article className="panel funnel-panel overview-funnel"><header className="panel-header"><div><h2>客户旅程漏斗</h2><p>留资与成交仅统计平台上线后的标签事件</p></div><button className="text-button" onClick={() => onNavigate('conversations')}>查看会话<ArrowRight size={14} /></button></header><div className="funnel-list">{(funnel.data?.items ?? []).map((item) => { const total = funnel.data?.items[0]?.value || 0; const rate = total ? Math.round(item.value / total * 100) : 0; return <div className="funnel-item" key={item.name}><div className="funnel-meta"><span>{item.name}</span><strong>{item.value}</strong></div><div className="progress-track"><span style={{ width: `${rate}%` }} /></div><div className="funnel-rate">{rate}%</div></div>; })}</div></article>
      </section>
    </>}
  </div>;
}
