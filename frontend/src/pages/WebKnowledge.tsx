import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  Activity, Ban, BookOpenText, CheckCircle2, ExternalLink, FileSearch, Globe2,
  RefreshCw, Search, ShieldCheck,
} from 'lucide-react';
import { api, ApiError } from '../api';
import { Badge, EmptyState, PageHeader } from '../components';

type KnowledgeFact = { id?: string; text: string; source_url: string; source_quote: string; verified_at: string };
type KnowledgeModule = {
  kind: 'knowledge_module' | 'runtime_knowledge_module'; key: string; title: string; summary: string;
  topics: string[]; facts: KnowledgeFact[]; runtime_scope?: 'live_and_playground'; version?: string;
  fixed_answers?: { id: string; name: string; status: string; answer_text: string; source_ref: string; positive_examples: string[] }[];
};
type RuntimeModulesResponse = { items: KnowledgeModule[]; summary: { modules: number; facts: number; scope: string } };
type InventoryItem = {
  url: string; title?: string; site_section: string; global_candidate: boolean;
  status: 'fetched' | 'failed';
};
type ExcludedItem = { category: string; reason: string };
type Revision = {
  id: number; revision_number: number; content_hash: string;
  status: 'pending_review' | 'active' | 'superseded'; fetched_at: string;
  knowledge_module_count: number; fact_count: number;
  knowledge_modules?: KnowledgeModule[]; site_inventory?: InventoryItem[];
  excluded_items?: ExcludedItem[];
};
type Source = {
  id: number; name: string; url: string; description: string;
  status: 'active' | 'paused' | 'archived'; ai_enabled: boolean;
  runtime_scope: 'disabled' | 'playground' | 'live';
  sync_status: 'never' | 'syncing' | 'ready' | 'failed'; last_error: string | null;
  last_checked_at: string | null; latest_revision: Revision | null;
  published_revision: Revision | null;
};
type SourcesResponse = {
  items: Source[];
  capabilities: { refresh_enabled: boolean; publish_enabled: boolean };
};
type UsageItem = {
  key: string; environment: 'live' | 'playground'; conversation_id: number | null;
  session_id: number | null; run_id: number; module: string; status: string;
  outbound: boolean; created_at: string; completed_at: string | null;
  usage: {
    status: 'used' | 'retrieved_not_used' | 'no_match' | 'disabled';
    retrieved_fact_count: number; retrieved_fact_ids: string[];
    used_fact_count: number; used_fact_ids: string[];
    verification_passed: boolean | null; response_source: string;
  };
};
type UsageResponse = {
  items: UsageItem[];
  summary: { total: number; used: number; retrieved_not_used: number; no_match: number; disabled: number };
};
type View = 'modules' | 'usage' | 'coverage' | 'excluded';

function formatTime(value?: string | null) {
  if (!value) return '尚未整理';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-TW', { hour12: false });
}

export default function WebKnowledge() {
  const queryClient = useQueryClient();
  const [view, setView] = useState<View>('modules');
  const [search, setSearch] = useState('');
  const sources = useQuery({
    queryKey: ['web-knowledge-sources'],
    queryFn: () => api<SourcesResponse>('/knowledge/web-sources'),
  });
  const source = sources.data?.items[0];
  const revisionId = source?.published_revision?.id ?? source?.latest_revision?.id ?? null;
  const revision = useQuery({
    queryKey: ['web-knowledge-revision', revisionId],
    queryFn: () => api<Revision>(`/knowledge/web-revisions/${revisionId}`),
    enabled: revisionId != null,
  });
  const usage = useQuery({
    queryKey: ['web-knowledge-usage', source?.id],
    queryFn: () => api<UsageResponse>(`/knowledge/web-sources/${source!.id}/usage?limit=100`),
    enabled: source?.id != null,
  });
  const runtimeModules = useQuery({
    queryKey: ['runtime-knowledge-modules'],
    queryFn: () => api<RuntimeModulesResponse>('/knowledge/runtime-modules'),
  });
  const changeUsage = useMutation({
    mutationFn: async (action: 'enable' | 'disable') => {
      if (!source) throw new Error('尚未建立官网知识');
      if (action === 'enable') {
        const target = source.published_revision ?? source.latest_revision;
        if (!target) throw new Error('没有可启用的版本');
        return api<Source>(`/knowledge/web-sources/${source.id}/revisions/${target.id}/publish?runtime_scope=${usageScope}`, { method: 'POST' });
      }
      return api<Source>(`/knowledge/web-sources/${source.id}/disable`, { method: 'POST' });
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['web-knowledge-sources'] });
      await queryClient.invalidateQueries({ queryKey: ['web-knowledge-usage'] });
    },
  });
  const websiteModules = revision.data?.knowledge_modules ?? [];
  const modules = [...(runtimeModules.data?.items ?? []), ...websiteModules];
  const inventory = revision.data?.site_inventory ?? [];
  const excluded = revision.data?.excluded_items ?? [];
  const filteredModules = useMemo(() => {
    const query = search.trim().toLowerCase();
    if (!query) return modules;
    return modules.filter(item => `${item.title} ${item.summary} ${item.topics.join(' ')} ${item.facts.map(fact => fact.text).join(' ')}`.toLowerCase().includes(query));
  }, [modules, search]);
  const error = sources.error || revision.error || runtimeModules.error;
  const knowledgeEnabled = !!source?.ai_enabled && source.runtime_scope !== 'disabled';
  const [usageScope, setUsageScope] = useState<'playground' | 'live'>('live');
  useEffect(() => { if (source?.runtime_scope === 'playground' || source?.runtime_scope === 'live') setUsageScope(source.runtime_scope); }, [source?.runtime_scope]);

  useEffect(() => {
    if (view !== 'modules') setSearch('');
  }, [view]);

  return <div className="page-content web-knowledge-page global-library-page">
    <PageHeader
      eyebrow="GLOBAL KNOWLEDGE"
      title="全局官网知识库"
      description="从 China2Go 官网整理、核对并分模块保存的共用文字知识。线路价格、团期、行程与图片仍在线路管理中维护。"
      actions={<div className="knowledge-disabled-actions">
        <select aria-label="知识使用范围" value={usageScope} onChange={event => setUsageScope(event.target.value as 'live' | 'playground')}><option value="live">真实接待与演练</option><option value="playground">仅演练</option></select>
        {knowledgeEnabled && usageScope !== source?.runtime_scope && <button className="primary-button" onClick={() => changeUsage.mutate('enable')}>保存范围</button>}
        <button className={knowledgeEnabled ? 'secondary-button' : 'primary-button'} disabled={!source || changeUsage.isPending || (!knowledgeEnabled && !sources.data?.capabilities.publish_enabled)} onClick={() => changeUsage.mutate(knowledgeEnabled ? 'disable' : 'enable')}><BookOpenText size={15} />{changeUsage.isPending ? '处理中…' : knowledgeEnabled ? '停止使用' : '启用知识'}</button>
      </div>}
    />

    <div className="knowledge-library-meta"><Badge tone={knowledgeEnabled ? 'green' : 'neutral'}>{knowledgeEnabled ? source?.runtime_scope === 'live' ? '真实接待与演练使用中' : '仅演练使用中' : '已停用'}</Badge><span>已发布版本 {source?.published_revision?.revision_number ?? '—'}</span></div>
    {changeUsage.error ? <div className="config-error"><ShieldCheck size={16} />{(changeUsage.error as Error).message}</div> : null}
    {error ? <div className="config-error"><ShieldCheck size={16} />{(error as ApiError).message}</div> : null}
    {sources.isLoading || (revisionId && revision.isLoading) ? <EmptyState type="loading" title="正在读取全局知识" description="" /> : !source || !revision.data ? <EmptyState title="尚未建立全局知识" description="当前没有可检查的官网知识版本。" /> : <>
      <section className="knowledge-library-summary">
        <div className="knowledge-library-source"><span className="knowledge-library-icon"><Globe2 size={21} /></span><div><strong>{source.name}</strong><a href="https://china2go.com/" target="_blank" rel="noreferrer">https://china2go.com/ <ExternalLink size={11} /></a><small>{source.description}</small></div><Badge tone={knowledgeEnabled ? 'blue' : 'amber'}>{knowledgeEnabled ? (source.runtime_scope === 'live' ? '真实接待与演练' : '仅演练') : '未启用'}</Badge></div>
        <div className="knowledge-library-stats">
          <article><span>知识模块</span><strong>{modules.length}</strong><small>{runtimeModules.data?.summary.modules ?? 0} 个运行核心模块</small></article>
          <article><span>已核对事实</span><strong>{revision.data.fact_count + (runtimeModules.data?.summary.facts ?? 0)}</strong><small>每条保留来源与范围</small></article>
          <article><span>全站页面</span><strong>{inventory.length}</strong><small>{inventory.filter(item => item.status === 'fetched').length} 页读取成功</small></article>
          <article><span>AI 状态</span><strong>{knowledgeEnabled ? (source.runtime_scope === 'live' ? '已启用' : '仅演练') : '关闭'}</strong><small>{knowledgeEnabled ? (source.runtime_scope === 'live' ? '真实接待与演练读取' : '真实接待不读取') : '不参与回答'}</small></article>
        </div>
        <div className="knowledge-library-meta"><span>最近整理：{formatTime(source.last_checked_at)}</span><span>内容指纹：{revision.data.content_hash.slice(0, 16)}</span><span>来源语言：繁体中文</span></div>
      </section>

      <nav className="knowledge-library-tabs" aria-label="知识库视图">
        <button className={view === 'modules' ? 'active' : ''} onClick={() => setView('modules')}><BookOpenText size={15} />知识模块</button>
        <button className={view === 'usage' ? 'active' : ''} onClick={() => setView('usage')}><Activity size={15} />使用记录</button>
        <button className={view === 'coverage' ? 'active' : ''} onClick={() => setView('coverage')}><FileSearch size={15} />全站覆盖</button>
        <button className={view === 'excluded' ? 'active' : ''} onClick={() => setView('excluded')}><ShieldCheck size={15} />排除与边界</button>
      </nav>

      {view === 'modules' ? <section className="knowledge-modules-section">
        <div className="knowledge-module-toolbar"><div className="route-search"><Search size={15} /><input value={search} onChange={event => setSearch(event.target.value)} placeholder="搜索知识主题或客户问题" /></div><span>{filteredModules.length} 个模块</span></div>
        <div className="knowledge-module-list">{filteredModules.map(module => <article key={module.key}>
          <header><div><h2>{module.title}</h2><p>{module.summary}</p></div><Badge tone={module.runtime_scope ? 'blue' : 'green'}>{module.runtime_scope ? '真实与演练均使用' : '官网已核对'}</Badge></header>
          <div className="knowledge-topic-list">{module.topics.map(topic => <span key={topic}>{topic}</span>)}</div>
          {module.runtime_scope ? <p>运行核心内容随审核版本发布，不受官网资料停用按钮控制；真实发送仍须通过接待门禁。</p> : null}
          {module.fixed_answers?.map(answer => <details key={answer.id}><summary>{answer.name} · {answer.status === 'active' ? '已审核启用' : '未启用'}</summary><p>{answer.answer_text}</p><small>来源：{answer.source_ref} · 问法：{answer.positive_examples.join('、')}</small></details>)}
          <div className="knowledge-fact-list">{module.facts.map((fact, index) => <div key={fact.id ?? `${module.key}-${index}`}><CheckCircle2 size={15} /><div><p>{fact.text}</p>{fact.source_url ? <a href={fact.source_url} target="_blank" rel="noreferrer">查看来源依据 <ExternalLink size={10} /></a> : <span>平台已审核规则</span>}<small>核对日期：{fact.verified_at}{module.version ? ` · ${module.version}` : ''}</small></div></div>)}</div>
        </article>)}</div>
      </section> : null}

      {view === 'usage' ? <section className="knowledge-usage-section">
        <header><div><h2>每次回复的官网知识使用情况</h2><p>同时记录 AI 演练和真实接待。只显示知识命中与使用结果，不展示客户联系方式。</p></div><button className="secondary-button compact" onClick={() => usage.refetch()}><RefreshCw size={14} />刷新</button></header>
        <div className="knowledge-usage-stats">
          <article><span>已记录回复</span><strong>{usage.data?.summary.total ?? 0}</strong></article>
          <article><span>实际采用知识</span><strong>{usage.data?.summary.used ?? 0}</strong></article>
          <article><span>命中但未采用</span><strong>{usage.data?.summary.retrieved_not_used ?? 0}</strong></article>
          <article><span>未启用或未命中</span><strong>{(usage.data?.summary.disabled ?? 0) + (usage.data?.summary.no_match ?? 0)}</strong></article>
        </div>
        {usage.isLoading ? <EmptyState type="loading" title="正在读取使用记录" description="" /> : !usage.data?.items.length ? <EmptyState title="暂无使用记录" description="当前知识尚未启用；启用后，每轮 AI 演练和真实回复都会记录在这里。" /> : <div className="knowledge-usage-table"><table><thead><tr><th>时间</th><th>环境</th><th>回复</th><th>知识结果</th><th>检索 / 采用</th><th>事实核验</th></tr></thead><tbody>{usage.data.items.map(item => <tr key={item.key}>
          <td>{formatTime(item.created_at)}</td>
          <td><Badge tone={item.environment === 'live' ? 'green' : 'blue'}>{item.environment === 'live' ? `真实 #${item.conversation_id}` : `演练 #${item.session_id}`}</Badge></td>
          <td>{item.module === 'reply' ? '实时回复' : '沉默跟进'}<small>{item.status}{item.outbound ? ' · 已发送' : ' · 未真实发送'}</small></td>
          <td><UsageStatus value={item.usage.status} /></td>
          <td>{item.usage.retrieved_fact_count} / {item.usage.used_fact_count}<small>{item.usage.used_fact_ids.join('、') || '没有采用官网事实'}</small></td>
          <td>{item.usage.verification_passed == null ? '未执行模型核验' : item.usage.verification_passed ? '通过' : '未通过'}<small>{item.usage.response_source}</small></td>
        </tr>)}</tbody></table></div>}
      </section> : null}

      {view === 'coverage' ? <section className="knowledge-coverage-section">
        <header><h2>官网繁体中文页面清单</h2><p>已依据官网站点地图盘点。具体线路、攻略文章和分类页会保留在清单中，但不会自动变成全局事实。</p></header>
        <div className="knowledge-coverage-table"><table><thead><tr><th>网页</th><th>栏目</th><th>读取状态</th><th>全局候选</th></tr></thead><tbody>{inventory.map(item => <tr key={`${item.site_section}-${item.url}`}><td><a href={item.url} target="_blank" rel="noreferrer"><strong>{item.title || item.url}</strong><small>{item.url}</small></a></td><td>{item.site_section}</td><td><Badge tone={item.status === 'fetched' ? 'green' : 'red'}>{item.status === 'fetched' ? '已读取' : '失败'}</Badge></td><td>{item.global_candidate ? '是' : '否'}</td></tr>)}</tbody></table></div>
      </section> : null}

      {view === 'excluded' ? <section className="knowledge-excluded-section">
        <header><h2>不会进入全局知识的内容</h2><p>这些内容可能出现在官网，但不适合跨线路直接回答，或需要更高等级的即时核对。</p></header>
        <div>{excluded.map(item => <article key={item.category}><ShieldCheck size={17} /><div><strong>{item.category}</strong><p>{item.reason}</p></div></article>)}</div>
      </section> : null}
    </>}
  </div>;
}

function UsageStatus({ value }: { value: UsageItem['usage']['status'] }) {
  const labels = { used: '已采用', retrieved_not_used: '命中未采用', no_match: '没有命中', disabled: '知识未启用' };
  return <Badge tone={value === 'used' ? 'green' : value === 'retrieved_not_used' ? 'blue' : 'amber'}>{labels[value]}</Badge>;
}
