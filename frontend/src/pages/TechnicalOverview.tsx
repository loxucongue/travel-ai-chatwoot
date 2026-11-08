import { useQuery } from '@tanstack/react-query';
import { api } from '../api';

type Distribution = { samples: number; mean: number | null; p50: number | null; p95: number | null };
type Count = { name: string; value: number };
type Report = {
  runs: number; missing_call_records: number;
  handoffs: { total: number; assist_allowed: number; reasons: Count[] };
  calls: { total: number; failed: number; failure_rate: number | null; unfinished: number; fallback: number; recovered_replies: number; reasons: Count[]; latency: Distribution };
  reply: { sent: number; latency: Distribution; processing_latency: Distribution };
  runs_by_status: Count[]; delivery: Record<string, number>;
  models: { provider: string; model: string; calls: number; failed: number; latency: Distribution }[];
};
const duration = (n: number | null) => n == null ? '—' : n < 60 ? `${n.toFixed(1)}秒` : `${(n / 60).toFixed(1)}分钟`;
const states: Record<string, string> = { completed: '已完成处理', failed: '处理失败', cancelled: '已取消', processing: '处理中', pending: '待处理' };
function Breakdown({ rows }: { rows: Count[] }) {
  return rows.length ? <div className="bi-bars">{rows.map(r => <div key={r.name}><span>{r.name}</span><i><b style={{ width: `${r.value / Math.max(1, ...rows.map(x => x.value)) * 100}%` }} /></i><strong>{r.value}</strong></div>)}</div> : <p>暂无记录</p>;
}
export default function TechnicalOverview({ start, end, inbox }: { start: string; end: string; inbox: string }) {
  const query = new URLSearchParams({ start, end, ...(inbox ? { inbox_id: inbox } : {}) }).toString();
  const report = useQuery({ queryKey: ['bi-technical', query], queryFn: () => api<Report>(`/bi/technical?${query}`), refetchInterval: 60000, retry: 1 });
  const data = report.data;
  return <section className="bi-technical" aria-label="运行质量">
    <div className="bi-context"><h2>运行质量</h2><span>按所选日期和渠道统计 · 不随“新客后续进展”切换</span><button className="text-button" disabled={report.isFetching} onClick={() => void report.refetch()}>{report.isFetching ? '读取中…' : '刷新'}</button></div>
    {report.isError ? <div className="bi-notice" role="alert">运行数据读取失败，请重试。</div> : !data ? <p role="status">正在读取运行数据…</p> : <>
      <div className="bi-cards bi-tech-cards">
        {[
          ['人工交接', data.handoffs.total, `其中 ${data.handoffs.assist_allowed} 次允许 AI 继续协助`],
          ['模型调用失败', data.calls.failed, `${data.calls.total} 次调用 · 失败率 ${data.calls.failure_rate == null ? '—' : `${data.calls.failure_rate}%`}`],
          ['平均首条回复', duration(data.reply.latency.mean), `${data.reply.latency.samples} 次有效样本`],
          ['P50 首条回复', duration(data.reply.latency.p50), '一半回复在此时间内发出'],
          ['P95 首条回复', duration(data.reply.latency.p95), '95% 回复在此时间内发出'],
          ['失败后仍发出回复', data.calls.recovered_replies, `备用调用 ${data.calls.fallback} 次`],
        ].map(([name, value, hint]) => <div className="bi-card" key={name}><span>{name}</span><strong>{value}</strong><small>{hint}</small></div>)}
      </div>
      <div className="bi-grid"><article className="panel bi-panel"><h2>交接原因</h2><Breakdown rows={data.handoffs.reasons} /></article><article className="panel bi-panel"><h2>调用失败原因</h2><Breakdown rows={data.calls.reasons} /></article></div>
      <details className="panel bi-definitions"><summary>模型表现与统计说明</summary>
        <div className="bi-tech-table"><table><thead><tr><th>模型</th><th>调用</th><th>失败</th><th>平均耗时</th><th>P95</th></tr></thead><tbody>{data.models.map(m => <tr key={`${m.provider}:${m.model}`}><td>{m.model}<small>{m.provider}</small></td><td>{m.calls}</td><td>{m.failed}</td><td>{duration(m.latency.mean)}</td><td>{duration(m.latency.p95)}</td></tr>)}</tbody></table></div>
        <div className="bi-pills">{data.runs_by_status.map(r => <span key={r.name}>{states[r.name] || '其他处理状态'} {r.value}</span>)}<span>发送失败 {data.delivery.failed || 0} 条</span><span>发送回执不明 {data.delivery.submission_unknown || 0} 条</span></div>
        <p>首条回复：本轮最早客户消息至渠道接收第一条回复，包含排队与重试；分段图文只计一次，不代表已读。共 {data.reply.sent} 轮发出回复，其中 {data.reply.sent - data.reply.latency.samples} 轮缺少起始时间。主动跟进不计入回复耗时。</p>
        <p>调用按实际请求计数，重试和备用均计入；失败后恢复仍保留失败次数。处理失败可能继续重试，不等于最终转人工。处理平均耗时 {duration(data.reply.processing_latency.mean)}，模型调用平均耗时 {duration(data.calls.latency.mean)}。</p>
        <p>仅统计正式 V3 接待；调用按所属处理轮次的开始日期归属。共 {data.runs} 轮，{data.missing_call_records} 轮缺少调用记录，{data.calls.unfinished} 次调用结果未记录。空样本显示“—”；P50／P95 取排序后的对应分位样本。人工交接按任务创建时间计次，包含顾问通知，允许 AI 协助不代表完全停用 AI。</p>
      </details>
    </>}
  </section>;
}
