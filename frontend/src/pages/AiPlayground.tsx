import { useMemo, useRef, useState } from 'react';
import { useMutation } from '@tanstack/react-query';
import {
  Bot, CheckCircle2, CircleGauge, Clock3, FlaskConical, RotateCcw,
  Send, ShieldCheck, Sparkles, UserRound, Waypoints,
} from 'lucide-react';
import { api, ApiError } from '../api';
import { Badge, PageHeader } from '../components';

type Scenario = 'auto' | 'peach_11d' | 'peach_9d' | 'private_group' | 'other_peak' | 'other_no_peak' | 'other_destination';
type ChatRole = 'customer' | 'assistant';

interface PlaygroundReply {
  action: 'reply' | 'handoff' | 'no_action';
  branch: string;
  branch_name: string;
  intent: string;
  reply: string | null;
  slots: Record<string, unknown>;
  missing_slots: string[];
  handoff_reason: string | null;
  evidence_refs: string[];
  safety_flags: string[];
  confidence: number;
  elapsed_ms: number;
  browser_elapsed_ms: number;
  model: string;
  prompt_version: string;
  attempts: number;
  input_tokens: number | null;
  output_tokens: number | null;
  outbound: false;
  engine_version: 'v1' | 'v2';
  engine_release_id?: string | null;
  environment?: string;
  knowledge_versions?: Record<string, string>;
  model_http_request_count?: number;
  loaded_skills?: string[];
  tool_calls?: { name: string; status: string }[];
}

interface PlaygroundMessage {
  id: string;
  role: ChatRole;
  content: string;
  result?: PlaygroundReply;
}

const scenarios: { key: Scenario; name: string; description: string; example: string }[] = [
  { key: 'auto', name: '自动识别', description: '按完整对话判断业务分支', example: '想了解一下你们的西藏桃花行程' },
  { key: 'peach_11d', name: '桃花 + 珠峰 11 日', description: '完整线路咨询与需求收集', example: '11天桃花加珠峰的行程怎么安排？' },
  { key: 'peach_9d', name: '林芝桃花 9 日', description: '不上珠峰的桃花线路', example: '9日桃花路线适合带父母参加吗？' },
  { key: 'private_group', name: '自组包团', description: '收集人数、日期与预算', example: '我们公司有16个人，想在三月底包团' },
  { key: 'other_peak', name: '其他时间上珠峰', description: '非桃花档期的珠峰需求', example: '十月份想去珠峰，有什么路线？' },
  { key: 'other_no_peak', name: '其他时间不上珠峰', description: '常规西藏线路需求', example: '暑假去西藏但不去珠峰，有推荐吗？' },
  { key: 'other_destination', name: '其他目的地', description: '收集基本需求后转人工', example: '你们有云南的私人团吗？' },
];

const actionNames = { reply: '生成回复', handoff: '转人工', no_action: '不处理' };
const intentNames: Record<string, string> = {
  route_intro: '线路介绍', price: '价格咨询', departure: '团期咨询', itinerary: '行程咨询',
  contact: '联系方式', complaint: '客诉', other: '其他',
};
const slotNames: Record<string, string> = {
  party_size: '出行人数', departure_window: '出发时间', budget: '预算', destination: '目的地',
};
const reasonNames: Record<string, string> = {
  complaint_or_human_requested: '客户要求人工或涉及客诉', business_content_incomplete: '当前分支缺少已审核业务内容',
  unsafe_generated_claim: '模型回复包含未核验事实', live_price: '涉及实时价格', availability: '涉及实时余位',
  departure_confirmation: '涉及实时团期', health_or_permit: '涉及健康或证件政策', contract: '涉及合同承诺',
};

const newId = () => `${Date.now()}-${Math.random().toString(16).slice(2)}`;

export default function AiPlayground() {
  const [engineVersion, setEngineVersion] = useState<'v1' | 'v2'>('v2');
  const [scenario, setScenario] = useState<Scenario>('auto');
  const [draft, setDraft] = useState('');
  const [messages, setMessages] = useState<PlaygroundMessage[]>([]);
  const streamRef = useRef<HTMLDivElement>(null);
  const selectedScenario = scenarios.find((item) => item.key === scenario) ?? scenarios[0];
  const results = messages.flatMap((item) => item.result ? [item.result] : []);
  const latest = results.at(-1);
  const sessionMetrics = useMemo(() => {
    const total = results.reduce((sum, item) => sum + item.elapsed_ms, 0);
    return {
      turns: results.length,
      average: results.length ? Math.round(total / results.length) : 0,
      handoffs: results.filter((item) => item.action === 'handoff').length,
      fastest: results.length ? Math.min(...results.map((item) => item.elapsed_ms)) : 0,
    };
  }, [results]);

  const mutation = useMutation({
    mutationFn: async ({ text, history }: { text: string; history: PlaygroundMessage[] }) => {
      const started = performance.now();
      const value = await api<Omit<PlaygroundReply, 'browser_elapsed_ms'>>('/evaluation/playground/reply', {
        method: 'POST',
        body: JSON.stringify({
          customer_message: text,
          scenario,
          messages: history.map((item) => ({ role: item.role, content: item.content })),
          engine_version: engineVersion,
        }),
      });
      return { ...value, browser_elapsed_ms: Math.round(performance.now() - started) };
    },
    onSuccess: (result) => {
      setMessages((current) => [...current, {
        id: newId(), role: 'assistant',
        content: result.reply || (result.action === 'no_action' ? '本轮不需要回复。' : '已建议转交人工顾问。'),
        result,
      }]);
      window.setTimeout(() => streamRef.current?.scrollTo({ top: streamRef.current.scrollHeight, behavior: 'smooth' }), 30);
    },
  });

  const send = (preset?: string) => {
    const text = (preset ?? draft).trim();
    if (!text || mutation.isPending) return;
    const history = messages;
    setMessages((current) => [...current, { id: newId(), role: 'customer', content: text }]);
    setDraft('');
    mutation.mutate({ text, history });
    window.setTimeout(() => streamRef.current?.scrollTo({ top: streamRef.current.scrollHeight, behavior: 'smooth' }), 30);
  };

  const reset = () => { setMessages([]); setDraft(''); mutation.reset(); };
  const selectScenario = (next: Scenario) => {
    if (next === scenario) return;
    setScenario(next);
    setMessages([]);
    setDraft('');
    mutation.reset();
  };

  return <div className="page-content playground-page">
    <PageHeader
      eyebrow="AI QUALITY LAB"
      title="AI 演练场"
      description="模拟真实客户对话，观察回复速度、业务判断与安全拦截。"
      actions={<div className="playground-safe-state"><ShieldCheck size={16} /><span>离线演练</span><strong>不会发送至 Chatwoot</strong></div>}
    />

    <section className="playground-metrics" aria-label="本轮演练指标">
      <div><span>已完成轮次</span><strong>{sessionMetrics.turns}</strong></div>
      <div><span>平均模型耗时</span><strong>{sessionMetrics.average ? `${(sessionMetrics.average / 1000).toFixed(1)}s` : '--'}</strong></div>
      <div><span>最快响应</span><strong>{sessionMetrics.fastest ? `${(sessionMetrics.fastest / 1000).toFixed(1)}s` : '--'}</strong></div>
      <div><span>建议转人工</span><strong>{sessionMetrics.handoffs}</strong></div>
      <div className="playground-mode"><span>当前模式</span><strong>{selectedScenario.name} · {engineVersion.toUpperCase()}</strong></div>
    </section>

    <section className="playground-shell">
      <aside className="scenario-panel">
        <header><Waypoints size={17} /><div><strong>业务场景</strong><span>为多轮对话固定上下文</span></div></header>
        <div className="scenario-list">
          <button className={engineVersion === 'v2' ? 'active' : ''} onClick={() => { setEngineVersion('v2'); reset(); }}><span>V2 当前版本</span><small>按线路与客户需求回复</small></button>
          <button className={engineVersion === 'v1' ? 'active' : ''} onClick={() => { setEngineVersion('v1'); reset(); }}><span>V1 历史版本</span><small>用于回看与对比测试</small></button>
          {scenarios.map((item) => <button key={item.key} className={scenario === item.key ? 'active' : ''} onClick={() => selectScenario(item.key)}>
            <span>{item.name}</span><small>{item.description}</small>
          </button>)}
        </div>
        <div className="scenario-example">
          <span>示例客户问题</span>
          <button onClick={() => send(selectedScenario.example)} disabled={mutation.isPending}>{selectedScenario.example}<Send size={13} /></button>
        </div>
      </aside>

      <div className="playground-chat">
        <header className="playground-chat-header">
          <div><span className="playground-avatar"><UserRound size={18} /></span><div><strong>模拟客户</strong><small>{selectedScenario.name}</small></div></div>
          <button className="secondary-button compact" onClick={reset} disabled={!messages.length && !mutation.error}><RotateCcw size={14} />重新开始</button>
        </header>
        <div className="playground-stream" ref={streamRef}>
          {!messages.length ? <div className="playground-welcome"><span><FlaskConical size={24} /></span><strong>开始一段模拟咨询</strong><p>选择左侧场景，或直接输入客户可能提出的问题。</p><div>{['想了解桃花行程', '你们这个行程多少钱？', '我要找真人客服'].map((item) => <button key={item} onClick={() => send(item)}>{item}</button>)}</div></div> : null}
          {messages.map((message) => <div className={`playground-message ${message.role}`} key={message.id}>
            <span className="playground-message-avatar">{message.role === 'customer' ? <UserRound size={15} /> : <Bot size={15} />}</span>
            <div><div className="playground-bubble">{message.content}</div>{message.result ? <div className="playground-message-meta"><span>{message.result.branch_name}</span><i />{actionNames[message.result.action]}<i />{(message.result.elapsed_ms / 1000).toFixed(1)}s</div> : null}</div>
          </div>)}
          {mutation.isPending ? <div className="playground-message assistant is-thinking"><span className="playground-message-avatar"><Bot size={15} /></span><div><div className="playground-bubble"><span /><span /><span /></div><div className="playground-message-meta">正在识别意图并校验业务规则</div></div></div> : null}
          {mutation.isError ? <div className="playground-error">{mutation.error instanceof ApiError ? mutation.error.message : '本次演练失败，请稍后重试'}</div> : null}
        </div>
        <footer className="playground-composer">
          <textarea value={draft} onChange={(event) => setDraft(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); send(); } }} placeholder="输入模拟客户消息..." rows={2} />
          <button className="primary-button icon-only" onClick={() => send()} disabled={!draft.trim() || mutation.isPending} aria-label="发送模拟消息" title="发送模拟消息"><Send size={17} /></button>
        </footer>
      </div>

      <aside className="diagnostics-panel">
        <header><CircleGauge size={17} /><div><strong>本轮诊断</strong><span>模型输出与规则校验结果</span></div></header>
        {!latest ? <div className="diagnostics-empty"><Sparkles size={23} /><span>等待首轮回复</span></div> : <div className="diagnostics-content">
          <div className="latency-readout"><div><span>模型耗时</span><strong>{(latest.elapsed_ms / 1000).toFixed(2)}<small>s</small></strong></div><Clock3 size={22} /><footer>端到端 {(latest.browser_elapsed_ms / 1000).toFixed(2)}s · {latest.attempts} 次请求</footer></div>
          <section><h3>决策结果</h3><dl><div><dt>动作</dt><dd><Badge tone={latest.action === 'reply' ? 'green' : latest.action === 'handoff' ? 'amber' : 'neutral'}>{actionNames[latest.action]}</Badge></dd></div><div><dt>业务分支</dt><dd>{latest.branch_name}</dd></div><div><dt>客户意图</dt><dd>{intentNames[latest.intent] ?? latest.intent}</dd></div></dl></section>
          <section><div className="diagnostic-heading"><h3>置信度</h3><strong>{Math.round(latest.confidence * 100)}%</strong></div><div className="confidence-track"><span style={{ width: `${latest.confidence * 100}%` }} /></div></section>
          <section><h3>已识别信息</h3><div className="diagnostic-tags">{Object.entries(latest.slots).filter(([, value]) => value).length ? Object.entries(latest.slots).filter(([, value]) => value).map(([key, value]) => <span key={key}>{slotNames[key] ?? key}: {String(value)}</span>) : <em>暂未识别</em>}</div></section>
          <section><h3>缺失信息</h3><div className="diagnostic-tags missing">{latest.missing_slots.length ? latest.missing_slots.map((item) => <span key={item}>{slotNames[item] ?? item}</span>) : <em><CheckCircle2 size={13} />无必填缺失</em>}</div></section>
          {(latest.handoff_reason || latest.safety_flags.length) ? <section className="safety-diagnostic"><h3>安全拦截</h3>{latest.handoff_reason ? <p>{reasonNames[latest.handoff_reason] ?? latest.handoff_reason}</p> : null}<div className="diagnostic-tags warning">{latest.safety_flags.map((item) => <span key={item}>{reasonNames[item] ?? item}</span>)}</div></section> : null}
          {latest.loaded_skills?.length ? <section><h3>已加载 Skills</h3><div className="diagnostic-tags">{latest.loaded_skills.map((item) => <span key={item}>{item}</span>)}</div></section> : null}
          <section className="model-meta"><h3>演练模型信息</h3><dl><div><dt>环境</dt><dd>{latest.environment ?? 'playground'}</dd></div><div><dt>实际模型请求</dt><dd>{latest.model_http_request_count ?? '未记录'}</dd></div><div><dt>知识版本</dt><dd>{Object.values(latest.knowledge_versions ?? {}).join(' / ') || '未记录'}</dd></div><div><dt>引擎</dt><dd>{latest.engine_version.toUpperCase()}</dd></div><div><dt>发布版本</dt><dd>{latest.engine_release_id ?? '未记录'}</dd></div><div><dt>模型</dt><dd>{latest.model}</dd></div><div><dt>Token</dt><dd>{latest.input_tokens ?? '--'} / {latest.output_tokens ?? '--'}</dd></div><div><dt>提示版本</dt><dd>{latest.prompt_version}</dd></div></dl></section>
        </div>}
      </aside>
    </section>
  </div>;
}
