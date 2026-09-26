import { useEffect, useMemo, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  AlertTriangle, ArrowLeft, BookOpenText, CheckCircle2, ChevronDown, ChevronRight,
  ChevronUp, Clock3, ExternalLink, FileCheck2, Image as ImageIcon, Import, ListOrdered,
  MapPinned, Plus, RefreshCw, Save, Search, ShieldCheck, Tags, Trash2, Upload, X,
} from 'lucide-react';
import { api, API_BASE, ApiError } from '../api';
import { Badge, EmptyState } from '../components';
import {
  clone, ConfigResponse, ContentDraft as BaseContentDraft, ContentGroup, groupLabels, normalizeConfig,
  FixedAnswer, ProductAsset, ProductFact, productDraft, ReceptionConfig, RouteProduct as BaseRouteProduct,
} from './reception-config';

type ContentDraft = BaseContentDraft & { delete_sop_group_keys?: string[] };
type RouteProduct = BaseRouteProduct & { policies?: Record<string, unknown> };

type RouteTab = 'base' | 'price' | 'content' | 'mainline' | 'answers' | 'assets' | 'versions';

function ProductStatus({ product, enabled }: { product: RouteProduct; enabled: boolean }) {
  if (!enabled) return <Badge>已停用</Badge>;
  if (product.readiness.ai_reply_ready && product.readiness.sop_ready) return <Badge tone="green">允许接待</Badge>;
  if (product.readiness.missing_assets.length) return <Badge tone="amber">资料缺失</Badge>;
  return <Badge tone="amber">待发布</Badge>;
}

function Switch({ checked, onChange, disabled = false }: { checked: boolean; onChange: (value: boolean) => void; disabled?: boolean }) {
  return <button type="button" className={`strategy-switch ${checked ? 'on' : ''}`} onClick={() => !disabled && onChange(!checked)} disabled={disabled} aria-pressed={checked}><span /></button>;
}

function formatTime(value?: string | null) {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false });
}

function isPriceOrSchedule(fact: ProductFact) {
  return /price|departure|applicability|價格|价格|出發|出发|團期|团期|檔期|档期/i.test(`${fact.id} ${fact.text}`);
}

export default function RouteProducts() {
  const queryClient = useQueryClient();
  const routeFile = useRef<HTMLInputElement>(null);
  const products = useQuery({ queryKey: ['route-products'], queryFn: () => api<{ items: RouteProduct[]; outbound: false }>('/automation/route-products') });
  const configQuery = useQuery({ queryKey: ['reception-config'], queryFn: () => api<ConfigResponse>('/automation/reception-config') });
  const [selected, setSelected] = useState('');
  const [tab, setTab] = useState<RouteTab>('base');
  const [draft, setDraft] = useState<ReceptionConfig | null>(null);
  const [savedConfig, setSavedConfig] = useState('');
  const [content, setContent] = useState<ContentDraft | null>(null);
  const [savedContent, setSavedContent] = useState('');
  const contentBase = useRef({ route: '', version: '' });
  const [search, setSearch] = useState('');
  const [statusFilter, setStatusFilter] = useState('all');
  const [notice, setNotice] = useState('');
  const [importing, setImporting] = useState(false);
  const current = products.data?.items.find(item => item.route_variant === selected);

  useEffect(() => {
    if (configQuery.data?.config && !draft) {
      const value = normalizeConfig(configQuery.data.config);
      setDraft(value); setSavedConfig(JSON.stringify(value));
    }
  }, [configQuery.data, draft]);
  useEffect(() => {
    if (current) {
      if (contentBase.current.route === selected && content && JSON.stringify(content) !== savedContent) return;
      const value = productDraft(current);
      contentBase.current = { route: selected, version: current.package_version };
      setContent(value); setSavedContent(JSON.stringify(value));
    }
  }, [selected, current?.package_version]);

  const configDirty = !!draft && JSON.stringify(draft) !== savedConfig;
  const contentDirty = !!content && JSON.stringify(content) !== savedContent;
  const enabledRoutes = draft?.routing.enabled_route_variants ?? [];
  const filtered = useMemo(() => (products.data?.items ?? []).filter(product => {
    const enabled = enabledRoutes.includes(product.route_variant);
    const matchesSearch = !search.trim() || `${product.name} ${product.route_variant}`.toLowerCase().includes(search.trim().toLowerCase());
    const matchesStatus = statusFilter === 'all' || (statusFilter === 'enabled' ? enabled : statusFilter === 'disabled' ? !enabled : !product.readiness.ai_reply_ready);
    return matchesSearch && matchesStatus;
  }), [products.data, search, statusFilter, enabledRoutes]);

  const publish = useMutation({
    mutationFn: async () => {
      if (contentDirty) await api(`/automation/route-products/${selected}/content`, { method: 'PUT', body: JSON.stringify({ ...content, base_package_version: contentBase.current.version }) });
      if (configDirty) await api('/automation/reception-config', { method: 'PUT', body: JSON.stringify(draft) });
    },
    onSuccess: async () => {
      setNotice('线路新版本已发布。');
      if (content) setSavedContent(JSON.stringify(content));
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['route-products'] }),
        queryClient.invalidateQueries({ queryKey: ['reception-config'] }),
      ]);
      if (draft) setSavedConfig(JSON.stringify(draft));
    },
  });
  const importRoute = useMutation({
    mutationFn: (routePackage: Record<string, unknown>) => api<{ draft: { route_variant: string; name: string } }>('/automation/route-products/import', { method: 'POST', body: JSON.stringify({ package: routePackage }) }),
    onSuccess: data => { setNotice(`${data.draft.name} 已发布。`); setSelected(data.draft.route_variant); queryClient.invalidateQueries({ queryKey: ['route-products'] }); queryClient.invalidateQueries({ queryKey: ['reception-config'] }); },
  });
  const saveAsset = useMutation({
    mutationFn: ({ asset, values }: { asset: ProductAsset; values: AssetNarrativeDraft }) => api(`/automation/route-products/${selected}/assets/${encodeURIComponent(asset.key)}`, { method: 'PUT', body: JSON.stringify({
      display_name: values.name,
      usage: values.usage,
      what_it_shows: values.whatItShows,
      feature_points: lines(values.featurePoints),
      customer_value: values.customerValue,
      recommended_caption: values.recommendedCaption,
      avoid_claims: lines(values.avoidClaims),
    }) }),
    onSuccess: () => { setNotice('素材说明已保存。'); queryClient.invalidateQueries({ queryKey: ['route-products'] }); },
  });
  const replaceAsset = useMutation({
    mutationFn: async ({ asset, file }: { asset: ProductAsset; file: File }) => {
      const form = new FormData(); form.append('file', file);
      const media = await api<{ id: number }>('/media', { method: 'POST', body: form });
      return api<{ affected_routes: string[] }>(`/automation/route-products/${selected}/assets/${encodeURIComponent(asset.key)}/replace`, { method: 'POST', body: JSON.stringify({ media_id: media.id }) });
    },
    onSuccess: data => { setNotice(`素材已替换${data.affected_routes.length > 1 ? `，同步影响 ${data.affected_routes.length} 条线路` : ''}。`); queryClient.invalidateQueries({ queryKey: ['route-products'] }); },
  });

  function patchConfig(updater: (value: ReceptionConfig) => void) { if (!draft) return; const value = clone(draft); updater(value); setDraft(value); setNotice(''); }
  function patchContent(updater: (value: ContentDraft) => void) { if (!content) return; const value = clone(content); updater(value); setContent(value); setNotice(''); }
  async function importFile(file?: File) {
    if (!file) return; setImporting(true);
    try { await importRoute.mutateAsync(JSON.parse(await file.text()) as Record<string, unknown>); }
    catch (error) { setNotice(error instanceof SyntaxError ? 'JSON 文件格式错误。' : (error as Error).message); }
    finally { setImporting(false); if (routeFile.current) routeFile.current.value = ''; }
  }
  function openProduct(product: RouteProduct) { setSelected(product.route_variant); setTab('base'); setNotice(''); }

  const error = products.error || configQuery.error || publish.error || saveAsset.error || replaceAsset.error;
  if (products.isLoading || configQuery.isLoading) return <div className="page-content route-catalog-page"><section className="panel"><EmptyState type="loading" title="正在读取线路" description="" /></section></div>;
  if (!draft) return <div className="page-content route-catalog-page"><section className="panel"><EmptyState type="error" title="配置读取失败" description={(error as Error)?.message ?? '无法读取配置'} /></section></div>;

  return <div className="page-content route-catalog-page">
    <input ref={routeFile} type="file" accept="application/json,.json" hidden onChange={event => importFile(event.target.files?.[0])} />
    {notice ? <div className="config-notice"><CheckCircle2 size={16} /><span>{notice}</span><button onClick={() => setNotice('')}><X size={14} /></button></div> : null}
    {error ? <div className="config-error"><AlertTriangle size={16} />{(error as ApiError).message}</div> : null}

    {!selected || !current || !content ? <>
      <header className="management-page-head">
        <div><span className="eyebrow">ROUTE OPERATIONS</span><h1>线路管理</h1><p>维护线路资料、价格、AI 可用内容与图片素材。线路独立发布，不会修改全局 AI 接待策略。</p></div>
        <div><button className="secondary-button" onClick={() => routeFile.current?.click()} disabled={importing}><Import size={15} />{importing ? '正在校验…' : '导入线路包'}</button></div>
      </header>
      <section className="route-stat-grid">
        <article><span>全部线路</span><strong>{products.data?.items.length ?? 0}</strong><small>已接入平台</small><MapPinned /></article>
        <article><span>允许 AI 接待</span><strong>{enabledRoutes.length}</strong><small>受全局发送开关控制</small><CheckCircle2 /></article>
        <article><span>资料不完整</span><strong>{(products.data?.items ?? []).filter(item => !item.readiness.ai_reply_ready).length}</strong><small>发布前需要补齐</small><AlertTriangle /></article>
        <article><span>可用素材</span><strong>{(products.data?.items ?? []).reduce((sum, item) => sum + item.readiness.assets_ready, 0)}</strong><small>跨线路累计</small><ImageIcon /></article>
      </section>
      <section className="panel route-list-panel">
        <div className="route-list-toolbar"><div className="route-search"><Search size={16} /><input value={search} onChange={event => setSearch(event.target.value)} placeholder="搜索线路名称或编码" /></div><select value={statusFilter} onChange={event => setStatusFilter(event.target.value)}><option value="all">全部状态</option><option value="enabled">允许接待</option><option value="disabled">已停用</option><option value="incomplete">资料不完整</option></select><button className="secondary-button compact" onClick={() => products.refetch()}><RefreshCw size={14} />刷新</button></div>
        {!filtered.length ? <EmptyState title="没有匹配线路" description="调整搜索或筛选条件后重试。" /> : <div className="route-list-table-wrap"><table className="route-list-table"><thead><tr><th>线路</th><th>线上版本</th><th>资料完整度</th><th>AI 接待状态</th><th>沉默旅程</th><th>操作</th></tr></thead><tbody>{filtered.map(product => { const enabled = enabledRoutes.includes(product.route_variant); return <tr key={product.route_variant}><td><strong>{product.name}</strong><code>{product.route_variant}</code></td><td><Badge tone="blue">{product.package_version}</Badge></td><td><div className="route-readiness-line"><span>事实 {product.knowledge_facts.length}</span><span>图片 {product.readiness.assets_ready}/{product.readiness.assets_total}</span></div></td><td><ProductStatus product={product} enabled={enabled} /></td><td>{product.readiness.sop_ready ? <Badge tone="green">{product.default_sop.nodes.length} 个节点</Badge> : <Badge tone="amber">未就绪</Badge>}</td><td><button className="text-button" onClick={() => openProduct(product)}>配置 <ChevronRight size={14} /></button></td></tr>; })}</tbody></table></div>}
      </section>
    </> : <RouteDetail
      product={current} content={content} tab={tab} setTab={setTab}
      enabled={enabledRoutes.includes(selected)} dirty={contentDirty || configDirty}
      publishing={publish.isPending} onBack={() => setSelected('')}
      onPublish={() => publish.mutate()} onPatchContent={patchContent}
      onToggleEnabled={checked => patchConfig(value => { value.routing.enabled_route_variants = checked ? Array.from(new Set([...value.routing.enabled_route_variants, selected])) : value.routing.enabled_route_variants.filter(item => item !== selected); })}
      assetBusy={saveAsset.isPending || replaceAsset.isPending}
      onSaveAsset={(asset, values) => saveAsset.mutate({ asset, values })}
      onReplaceAsset={(asset, file) => replaceAsset.mutate({ asset, file })}
    />}
  </div>;
}

function RouteDetail({ product, content, tab, setTab, enabled, dirty, publishing, onBack, onPublish, onPatchContent, onToggleEnabled, assetBusy, onSaveAsset, onReplaceAsset }: {
  product: RouteProduct; content: ContentDraft; tab: RouteTab; setTab: (tab: RouteTab) => void;
  enabled: boolean; dirty: boolean; publishing: boolean; onBack: () => void; onPublish: () => void;
  onPatchContent: (updater: (value: ContentDraft) => void) => void; onToggleEnabled: (value: boolean) => void;
  assetBusy: boolean; onSaveAsset: (asset: ProductAsset, values: AssetNarrativeDraft) => void; onReplaceAsset: (asset: ProductAsset, file: File) => void;
}) {
  const tabs: [RouteTab, string][] = [['base', '基础信息'], ['price', '价格与档期'], ['content', '线路资料'], ['mainline', '接待主线'], ['answers', '固定问答'], ['assets', '图片与文件'], ['versions', '版本记录']];
  return <>
    <header className="route-detail-head panel">
      <div><button className="back-link" onClick={onBack}><ArrowLeft size={15} />返回线路列表</button><div className="route-detail-title"><h1>{content.name}</h1><ProductStatus product={product} enabled={enabled} />{dirty ? <Badge tone="amber">有未发布修改</Badge> : null}</div><p><code>{product.route_variant}</code><span>线上版本 {product.package_version}</span></p></div>
      <div className="route-detail-actions"><button className="primary-button" disabled={!dirty || publishing || !content.name.trim() || !content.selection_title.trim()} onClick={onPublish}><Save size={15} />{publishing ? '发布中…' : '校验并发布'}</button></div>
    </header>
    <nav className="route-detail-tabs panel">{tabs.map(([key, label]) => <button key={key} className={tab === key ? 'active' : ''} onClick={() => setTab(key)}>{label}</button>)}</nav>
    <section className="route-detail-panel panel">
      {tab === 'base' ? <BaseTab product={product} content={content} enabled={enabled} onToggleEnabled={onToggleEnabled} patch={onPatchContent} /> : null}
      {tab === 'price' ? <PriceTab facts={content.knowledge_facts} patch={onPatchContent} /> : null}
      {tab === 'content' ? <ContentTab product={product} content={content} patch={onPatchContent} /> : null}
      {tab === 'mainline' ? <MainlineTab content={content} patch={onPatchContent} /> : null}
      {tab === 'answers' ? <FixedAnswersTab product={product} content={content} patch={onPatchContent} /> : null}
      {tab === 'assets' ? <AssetsTab routeVariant={product.route_variant} assets={product.assets} busy={assetBusy} onSave={onSaveAsset} onReplace={onReplaceAsset} /> : null}
      {tab === 'versions' ? <VersionsTab product={product} /> : null}
    </section>
  </>;
}

function BaseTab({ product, content, enabled, onToggleEnabled, patch }: { product: RouteProduct; content: ContentDraft; enabled: boolean; onToggleEnabled: (value: boolean) => void; patch: (updater: (value: ContentDraft) => void) => void }) {
  return <div className="route-detail-grid"><div className="route-form-card"><header>线路基础信息</header><div className="route-form-body"><div className="route-field-grid"><label>线路名称<input value={content.name} maxLength={200} onChange={event => patch(value => { value.name = event.target.value; })} /></label><label>客户选择名称<input value={content.selection_title} maxLength={20} onChange={event => patch(value => { value.selection_title = event.target.value; })} /></label><label>线路编码<input value={product.route_variant} disabled /></label><label>线路状态<span className="route-switch-field"><Switch checked={enabled} onChange={onToggleEnabled} />{enabled ? '允许 AI 接待' : '暂停接待'}</span></label><label className="full">演练默认客户消息<textarea rows={3} maxLength={1000} value={content.default_entry_message} onChange={event => patch(value => { value.default_entry_message = event.target.value; })} /></label><KeywordTagsEditor keywords={content.match_keywords} onChange={keywords => patch(value => { value.match_keywords = keywords; })} /></div></div></div><Readiness product={product} /></div>;
}

function KeywordTagsEditor({ keywords, onChange }: { keywords: string[]; onChange: (keywords: string[]) => void }) {
  const [draft, setDraft] = useState('');
  const normalized = draft.trim();
  const canAdd = !!normalized && normalized.length <= 40 && keywords.length < 50
    && !keywords.some(item => item.toLocaleLowerCase() === normalized.toLocaleLowerCase());
  function addKeyword() {
    if (!canAdd) return;
    onChange([...keywords, normalized]);
    setDraft('');
  }
  return <div className="route-keyword-editor full">
    <div className="route-keyword-heading"><Tags size={16} /><span><strong>线路关键词标签</strong><small>客户提到只属于这条线路的关键词时，会优先匹配；多条线路共享的词不会强制选线。</small></span></div>
    <div className="route-keyword-tags">{keywords.map(keyword => <span key={keyword}>{keyword}<button type="button" title={`删除关键词 ${keyword}`} aria-label={`删除关键词 ${keyword}`} onClick={() => onChange(keywords.filter(item => item !== keyword))}><X size={13} /></button></span>)}</div>
    <div className="route-keyword-add"><input value={draft} maxLength={40} placeholder="例如：珠峰大本營" aria-label="新增线路关键词" onChange={event => setDraft(event.target.value)} onKeyDown={event => { if (event.key === 'Enter') { event.preventDefault(); addKeyword(); } }} /><button type="button" className="secondary-button compact" disabled={!canAdd} onClick={addKeyword}><Plus size={14} />添加关键词</button></div>
  </div>;
}

function Readiness({ product }: { product: RouteProduct }) {
  return <aside className="route-readiness"><header>发布检查</header><div><p><span>线路事实</span><strong>{product.knowledge_facts.length} 条</strong></p><p><span>回复内容组</span><strong>{product.content_groups.length} 组</strong></p><p><span>图片素材</span><strong className={product.readiness.assets_ready === product.readiness.assets_total ? '' : 'warn'}>{product.readiness.assets_ready}/{product.readiness.assets_total}</strong></p><p><span>沉默旅程</span><strong className={product.readiness.sop_ready ? '' : 'warn'}>{product.readiness.sop_ready ? '已发布' : '未就绪'}</strong></p></div><footer>只有事实、引用关系和必需素材全部通过校验，线路才会显示为可接待。</footer></aside>;
}

function PriceTab({ facts, patch }: { facts: ProductFact[]; patch: (updater: (value: ContentDraft) => void) => void }) {
  const indexes = facts.map((fact, index) => ({ fact, index })).filter(({ fact }) => isPriceOrSchedule(fact));
  return <div className="route-section-stack"><header className="detail-section-intro"><div><h2>价格与可售档期</h2><p>这里直接维护价格、有效期和出发安排，AI 回复与沉默跟进使用同一份数据。</p></div></header>{indexes.length ? <div className="fact-table-editor">{indexes.map(({ fact, index }) => <FactRow key={fact.id} fact={fact} onChange={(field, value) => patch(draft => { draft.knowledge_facts[index][field] = value; })} />)}</div> : <EmptyState title="暂无价格或档期资料" description="请在线路资料的对应内容模块中新增。" />}</div>;
}

function ContentTab({ product, content, patch }: { product: RouteProduct; content: ContentDraft; patch: (updater: (value: ContentDraft) => void) => void }) {
  const [editingKey, setEditingKey] = useState('');
  const [adding, setAdding] = useState(false);
  const [purpose, setPurpose] = useState('');
  const [text, setText] = useState('');
  const [inSequence, setInSequence] = useState(false);
  const [groupError, setGroupError] = useState('');
  function addGroup(event: React.FormEvent) {
    event.preventDefault();
    if (!purpose.trim() || !text.trim() || content.content_groups.length >= 100) return;
    const key = `operator_${crypto.randomUUID().replaceAll('-', '')}`;
    patch(value => {
      value.content_groups.push({ key, purpose: purpose.trim(), approved_text: text.trim(),
        asset_keys: [], evidence_refs: [], sequence: null, initial_delivery: false, delivery_mode: 'text_only' });
      if (inSequence) value.content_sequence.push(key);
    });
    setAdding(false); setPurpose(''); setText(''); setInSequence(false); setGroupError(''); setEditingKey(key);
  }
  function removeGroup(group: ContentGroup, deleteSop: boolean) {
    if (referencesGroup(product.policies, group.key)) {
      setGroupError('不能删除：此内容组仍被线路策略引用，请先处理策略依赖。');
      return;
    }
    const remainingAssets = new Set(content.content_groups.filter(item => item.key !== group.key).flatMap(item => item.asset_keys));
    const removedAssets = new Set(group.asset_keys.filter(key => !remainingAssets.has(key)));
    const answers = content.fixed_answers.filter(answer => answer.content_group_key === group.key || answer.asset_ids.some(key => removedAssets.has(key)));
    if (answers.length) {
      setGroupError(`不能删除：固定回答“${answers.map(answer => answer.name).join('、')}”仍在引用此内容组，请先删除或改选对应资料。`);
      return;
    }
    if (content.content_groups.length <= 1) { setGroupError('至少保留一个内容组。'); return; }
    if (!window.confirm(`删除“${contentGroupLabel(group)}”？该组也会从发送顺序中移除。${deleteSop ? '同时删除该组同名的源 SOP 节点；旧版本快照不变。' : ''}保存发布后生效。`)) return;
    patch(value => {
      value.content_groups = value.content_groups.filter(item => item.key !== group.key);
      value.content_sequence = value.content_sequence.filter(key => key !== group.key);
      if (deleteSop && product.content_groups.some(item => item.key === group.key)) {
        value.delete_sop_group_keys = [...new Set([...(value.delete_sop_group_keys ?? []), group.key])];
      }
    });
    setEditingKey(''); setGroupError('');
  }
  const mainlineOrder = new Map(content.content_sequence.map((key, index) => [key, index]));
  const groups = [...content.content_groups].sort((a, b) => {
    const left = mainlineOrder.get(a.key);
    const right = mainlineOrder.get(b.key);
    return (left ?? 999) - (right ?? 999) || (groupLabels[a.key] ?? a.key).localeCompare(groupLabels[b.key] ?? b.key, 'zh-CN');
  });
  const editing = content.content_groups.find(group => group.key === editingKey) ?? null;
  const factsById = new Map(content.knowledge_facts.map(fact => [fact.id, fact]));

  return <div className="route-section-stack route-library-page">
    <header className="detail-section-intro"><div><h2>线路资料</h2><p>这里就是 AI 可用的线路内容。实时回复与沉默跟进共用这些资料，不再维护两套内容。</p></div><button type="button" className="text-button" disabled={content.content_groups.length >= 100} onClick={() => setAdding(true)}><Plus size={16} />新增内容组</button></header>
    {groupError ? <p role="alert">{groupError}</p> : null}
    {adding ? <form className="route-guidance-editor" style={{ gridTemplateColumns: 'minmax(0, 1fr)' }} onSubmit={addGroup}><label>名称与用途<input style={{ width: '100%', minWidth: 0 }} required maxLength={500} value={purpose} onChange={event => setPurpose(event.target.value)} /></label><label>参考内容<textarea required rows={4} maxLength={10000} value={text} onChange={event => setText(event.target.value)} /></label><label style={{ display: 'flex', alignItems: 'center' }}><input type="checkbox" checked={inSequence} onChange={event => setInSequence(event.target.checked)} />加入主线发送顺序</label><button type="submit" className="primary-button" disabled={!purpose.trim() || !text.trim()}><Plus size={16} />添加</button><button type="button" className="text-button" onClick={() => setAdding(false)}>取消</button></form> : null}
    <div className="route-guidance-editor"><ShieldCheck size={18} /><label><strong>这条线路的接待重点</strong><span>只填写这条线路独有的说明，全局语气和留资规则仍在“AI 接待策略”维护。</span><textarea rows={3} maxLength={3000} value={content.ai_guidance} onChange={event => patch(value => { value.ai_guidance = event.target.value; })} /></label></div>
    <div className="route-module-grid">{groups.map(group => {
      const position = mainlineOrder.get(group.key);
      return <button type="button" key={group.key} className="route-module-card" onClick={() => setEditingKey(group.key)}>
        <span className="route-module-icon">{position === undefined ? <BookOpenText size={18} /> : <ListOrdered size={18} />}</span>
        <span className="route-module-copy" style={{ overflowWrap: 'anywhere' }}><strong>{contentGroupLabel(group)}</strong><small>{group.purpose}</small><em>{position === undefined ? '按客户问题使用' : `主线第 ${position + 1} 步`} · {group.asset_keys.length} 张图片 · {group.evidence_refs.length} 项资料</em></span>
        <ChevronRight size={18} />
      </button>;
    })}</div>
    {editing ? <ContentDrawer
      key={editing.key}
      product={product}
      group={editing}
      factsById={factsById}
      onClose={() => setEditingKey('')}
      onDelete={deleteSop => removeGroup(editing, deleteSop)}
      deleteError={groupError}
      patch={patch}
    /> : null}
  </div>;
}

function contentGroupLabel(group: ContentGroup) { return groupLabels[group.key] ?? (group.purpose.trim() || '未命名内容组'); }

function referencesGroup(value: unknown, key: string): boolean {
  if (typeof value === 'string') return value === key;
  if (Array.isArray(value)) return value.some(item => referencesGroup(item, key));
  if (value && typeof value === 'object') return Object.entries(value).some(([field, child]) => field === key || referencesGroup(child, key));
  return false;
}

function ContentDrawer({ product, group, factsById, onClose, onDelete, deleteError, patch }: {
  product: RouteProduct; group: ContentGroup; factsById: Map<string, ProductFact>;
  onClose: () => void; patch: (updater: (value: ContentDraft) => void) => void;
  onDelete: (deleteSop: boolean) => void; deleteError: string;
}) {
  const [deleteSop, setDeleteSop] = useState(false);
  const linkedFacts = group.evidence_refs.map(ref => factsById.get(ref)).filter((fact): fact is ProductFact => !!fact);
  const systemRefs = group.evidence_refs.filter(ref => !factsById.has(ref));
  function addFact() {
    patch(value => {
      const index = value.content_groups.findIndex(item => item.key === group.key);
      const id = `operator.${product.route_variant}.${Date.now()}`;
      value.knowledge_facts.push({ id, text: '请填写这项线路资料', source_ref: 'operator.config' });
      value.content_groups[index].evidence_refs.push(id);
    });
  }
  function removeFact(id: string) {
    patch(value => {
      value.knowledge_facts = value.knowledge_facts.filter(fact => fact.id !== id);
      value.content_groups.forEach(item => { item.evidence_refs = item.evidence_refs.filter(ref => ref !== id); });
    });
  }
  return <div className="route-drawer-backdrop" role="presentation" onMouseDown={event => { if (event.currentTarget === event.target) onClose(); }}>
    <aside className="route-content-drawer" role="dialog" aria-modal="true" aria-label={`编辑${contentGroupLabel(group)}`}>
      <header><div style={{ minWidth: 0, overflowWrap: 'anywhere' }}><span>编辑线路资料</span><h3>{contentGroupLabel(group)}</h3></div><button className="icon-button" title="关闭" onClick={onClose}><X size={19} /></button></header>
      <div className="route-drawer-body">
        {deleteError ? <p role="alert">{deleteError}</p> : null}
        <label>名称与用途<input value={group.purpose} maxLength={500} onChange={event => patch(value => { const index = value.content_groups.findIndex(item => item.key === group.key); value.content_groups[index].purpose = event.target.value; })} /></label>
        <label>给客户介绍的参考内容<textarea rows={8} maxLength={10000} value={group.approved_text} onChange={event => patch(value => { const index = value.content_groups.findIndex(item => item.key === group.key); value.content_groups[index].approved_text = event.target.value; })} /></label>
        <section className="drawer-linked-section"><div><h4>支持资料</h4><button className="text-button" type="button" onClick={addFact}><Plus size={15} />新增资料</button></div><p>系统会自动关联，不需要填写事实编号。</p>
          {linkedFacts.map(fact => {
            const operatorFact = fact.source_ref === 'operator.config';
            return <article key={fact.id}><textarea rows={3} maxLength={4000} value={fact.text} onChange={event => patch(value => { const index = value.knowledge_facts.findIndex(item => item.id === fact.id); value.knowledge_facts[index].text = event.target.value; })} /><div><span>{operatorFact ? '运营补充' : '线路正式资料'}</span>{operatorFact ? <button className="icon-button danger-icon" title="删除资料" onClick={() => removeFact(fact.id)}><Trash2 size={15} /></button> : null}</div></article>;
          })}
          {systemRefs.map(ref => <article className="system-fact" key={ref}><span>系统通用接待规则</span><small>由平台统一维护，线路页面无需编辑。</small></article>)}
        </section>
        <section className="drawer-linked-section"><div><h4>关联图片</h4><span>{group.asset_keys.length} 张</span></div>{group.asset_keys.length ? <div className="drawer-asset-list">{group.asset_keys.map(key => { const asset = product.assets.find(item => item.key === key); return <div key={key}>{asset?.preview_url ? <img src={`${API_BASE}${asset.preview_url}`} alt={asset.display_name} /> : <ImageIcon size={22} />}<span>{asset?.display_name ?? key}</span></div>; })}</div> : <p>这个内容模块没有配置图片。</p>}</section>
      </div>
      <footer style={{ flexWrap: 'wrap' }}><label style={{ display: 'flex', alignItems: 'center', gap: 8, width: '100%', fontSize: 13 }}><input type="checkbox" checked={deleteSop} onChange={event => setDeleteSop(event.target.checked)} />同时删除该组同名的源 SOP 节点</label><button type="button" className="text-button" onClick={() => onDelete(deleteSop)}><Trash2 size={16} />删除内容组</button><button className="primary-button" onClick={onClose}><CheckCircle2 size={16} />完成编辑</button></footer>
    </aside>
  </div>;
}

function MainlineTab({ content, patch }: { content: ContentDraft; patch: (updater: (value: ContentDraft) => void) => void }) {
  function move(index: number, offset: -1 | 1) {
    patch(value => {
      const target = index + offset;
      if (target < 0 || target >= value.content_sequence.length) return;
      [value.content_sequence[index], value.content_sequence[target]] = [value.content_sequence[target], value.content_sequence[index]];
    });
  }
  const byKey = new Map(content.content_groups.map(group => [group.key, group]));
  const onDemand = content.content_groups.filter(group => !content.content_sequence.includes(group.key));
  const immediateCount = content.content_sequence.filter(key => byKey.get(key)?.initial_delivery).length;
  const greeting = byKey.get('advisor_greeting');
  return <div className="route-section-stack mainline-page">
    <header className="detail-section-intro"><div><h2>接待主线</h2><p>线路确认后，勾选的内容会按顺序分段发送；每一项可以独立设置文字与图片的先后顺序。</p></div><Badge tone={immediateCount ? 'blue' : 'amber'}>{immediateCount} 项立即发送</Badge></header>
    {greeting ? <label className="block-field opening-copy-field">线路确认承接语<textarea rows={3} maxLength={200} value={greeting.approved_text} onChange={event => patch(value => { const current = value.content_groups.find(item => item.key === 'advisor_greeting'); if (current) current.approved_text = event.target.value; })} /></label> : null}
    <div className="mainline-delivery-setting"><Clock3 size={18} /><div><strong>分段发送间隔</strong><span>用于图片、介绍文字和下一组主线之间，避免内容同时堆在一起。</span></div><label><input type="number" min={1} max={30} value={content.initial_delivery_interval_seconds} onChange={event => patch(value => { value.initial_delivery_interval_seconds = Math.max(1, Math.min(30, Number(event.target.value) || 1)); })} /><em>秒</em></label></div>
    <div className="mainline-priority-note"><AlertTriangle size={18} /><div><strong>主线是默认推进顺序，不会压过客户当前问题。</strong><span>客户问价格、酒店或车辆时先直接回答；投诉、停止联系和转人工优先处理。完成后再回到下一项未介绍内容。</span></div></div>
    <ol className="mainline-list">{content.content_sequence.map((key, index) => { const group = byKey.get(key); if (!group) return null; return <li key={key}><span className="mainline-number">{index + 1}</span><div><strong>{contentGroupLabel(group)}</strong><p>{group.purpose}</p><small>{group.asset_keys.length ? `${group.asset_keys.length} 张相关图片` : '纯文字内容'}</small></div><div className="mainline-delivery-controls"><label className="mainline-initial-toggle"><Switch checked={group.initial_delivery} onChange={checked => patch(value => { const current = value.content_groups.find(item => item.key === key); if (current) current.initial_delivery = checked; })} /><span><strong>确认线路后立即发送</strong><small>每段约 {content.initial_delivery_interval_seconds} 秒</small></span></label><select aria-label="图文发送顺序" value={group.delivery_mode} onChange={event => patch(value => { const current = value.content_groups.find(item => item.key === key); if (current) current.delivery_mode = event.target.value as typeof current.delivery_mode; })}><option value="text_only">只发文字</option><option value="assets_only">只发图片</option><option value="text_then_assets">先文字后图片</option><option value="assets_then_text">先图片后文字</option></select></div><div className="mainline-actions"><button className="icon-button" title="上移" disabled={index === 0} onClick={() => move(index, -1)}><ChevronUp size={18} /></button><button className="icon-button" title="下移" disabled={index === content.content_sequence.length - 1} onClick={() => move(index, 1)}><ChevronDown size={18} /></button></div></li>; })}</ol>
    <section className="on-demand-content"><h3>按客户问题使用的内容</h3><p>这些内容不占用主线顺序，只在客户提问或业务条件满足时使用。</p><div>{onDemand.map(group => <span key={group.key} style={{ maxWidth: '100%', overflowWrap: 'anywhere' }}>{contentGroupLabel(group)}<button type="button" className="icon-button" title={`加入主线：${contentGroupLabel(group)}`} disabled={content.content_sequence.length >= 100} onClick={() => patch(value => { if (!value.content_sequence.includes(group.key)) value.content_sequence.push(group.key); })}><Plus size={16} /></button></span>)}</div></section>
  </div>;
}

function FixedAnswersTab({ product, content, patch }: { product: RouteProduct; content: ContentDraft; patch: (updater: (value: ContentDraft) => void) => void }) {
  const [editingId, setEditingId] = useState('');
  const editing = content.fixed_answers.find(item => item.id === editingId) ?? null;
  function addAnswer() {
    const id = `operator_answer_${Date.now()}`;
    const group = content.content_groups.find(item => item.key === content.content_sequence[0]) ?? content.content_groups[0];
    patch(value => value.fixed_answers.push({
      id, name: '新固定问答', status: 'pending_review', priority: 100, topics: ['other'], party_size_min: null, party_size_max: null,
      content_group_key: group.key, answer_text: '请填写审核后要逐字发送给客户的内容。',
      fact_ids: [...group.evidence_refs], asset_ids: [], source_ref: 'operator.config', answer_origin: 'operator_approved',
      positive_examples: [], negative_examples: [],
    }));
    setEditingId(id);
  }
  return <div className="route-section-stack fixed-answer-page">
    <header className="detail-section-intro"><div><h2>标准问答</h2><p>客户问题明确命中后，系统会逐字发送这里的标准回答，不交给 AI 改写。</p></div><button className="secondary-button compact" onClick={addAnswer}><Plus size={14} />新增问答</button></header>
    <div className="fixed-answer-accuracy"><ShieldCheck size={18} /><div><strong>一条问答只有一段客户会收到的内容</strong><span>官网导入项直接使用官网原话；选择“已启用”并发布线路后立即参与匹配。识别分类、事实引用和匹配优先级由系统维护。</span></div></div>
    {!content.fixed_answers.length ? <EmptyState title="暂无标准问答" description="可从官网话术或已审核顾问回答建立线路专属问答。" /> : <div className="fixed-answer-list">{[...content.fixed_answers].sort((a, b) => b.priority - a.priority).map(answer => <button type="button" key={answer.id} onClick={() => setEditingId(answer.id)}><span><strong>{answer.name}</strong><small>{answer.answer_origin === 'website_verbatim' ? '官网原话' : '运营维护'} · {answer.positive_examples.length} 个客户问法</small></span><span>{answer.status === 'active' ? <Badge tone="green">已启用</Badge> : answer.status === 'pending_review' ? <Badge tone="amber">待审核</Badge> : <Badge>已停用</Badge>}<ChevronRight size={16} /></span></button>)}</div>}
    {editing ? <FixedAnswerDrawer product={product} content={content} answer={editing} patch={patch} onClose={() => setEditingId('')} /> : null}
  </div>;
}

const fixedAnswerTopicsByGroup: Record<string, string[]> = {
  itinerary_overview: ['route_intro', 'itinerary'],
  peach_highlights: ['highlights'],
  hotel_reference: ['hotel'],
  vehicle_reference: ['vehicle'],
  price_reference: ['price'],
  departure_reference: ['departure'],
  party_intro_solo: ['party_size'],
  party_intro_small: ['party_size'],
  party_intro_group: ['party_size'],
  landmarks: ['highlights'],
  zhaji: ['highlights'],
  accommodation_summary: ['hotel'],
  rongbuk_reference: ['rongbuk', 'hotel'],
};

function FixedAnswerDrawer({ product, content, answer, patch, onClose }: { product: RouteProduct; content: ContentDraft; answer: FixedAnswer; patch: (updater: (value: ContentDraft) => void) => void; onClose: () => void }) {
  const update = (updater: (item: FixedAnswer, draft: ContentDraft) => void) => patch(draft => {
    const item = draft.fixed_answers.find(value => value.id === answer.id);
    if (item) updater(item, draft);
  });
  const selectedGroup = content.content_groups.find(group => group.key === answer.content_group_key);
  return <div className="route-drawer-backdrop" role="presentation" onMouseDown={event => { if (event.currentTarget === event.target) onClose(); }}>
    <aside className="route-content-drawer fixed-answer-drawer" role="dialog" aria-modal="true" aria-label={`编辑${answer.name}`}>
      <header><div><span>编辑标准问答</span><h3>{answer.name}</h3></div><button className="icon-button" title="关闭" onClick={onClose}><X size={19} /></button></header>
      <div className="route-drawer-body">
        <div className="fixed-answer-enable"><div><strong>{answer.answer_origin === 'website_verbatim' ? '官网原话 · 逐字发送' : '运营标准回答 · 逐字发送'}</strong><span>系统只负责判断是否命中，不会改写下方内容。</span></div><Badge tone={answer.answer_origin === 'website_verbatim' ? 'blue' : undefined}>{answer.answer_origin === 'website_verbatim' ? '官网来源' : '运营维护'}</Badge></div>
        <div className="route-field-grid"><label>问答名称<input value={answer.name} maxLength={200} onChange={event => update(item => { item.name = event.target.value; })} /></label><label>生效状态<select value={answer.status} onChange={event => update(item => { item.status = event.target.value as FixedAnswer['status']; })}><option value="active">已启用</option><option value="pending_review">待审核</option><option value="disabled">已停用</option></select><small>选择“已启用”并发布线路后立即生效。</small></label></div>
        <label>对应线路资料<select value={answer.content_group_key} onChange={event => update((item, draft) => { item.content_group_key = event.target.value; const group = draft.content_groups.find(value => value.key === event.target.value); item.topics = fixedAnswerTopicsByGroup[event.target.value] ?? ['other']; item.fact_ids = [...(group?.evidence_refs ?? [])]; item.asset_ids = item.asset_ids.filter(id => group?.asset_keys.includes(id)); })}>{content.content_groups.map(group => <option key={group.key} value={group.key}>{contentGroupLabel(group)}</option>)}</select></label>
        <label>客户可能会这样问（每行一个）<textarea rows={5} value={answer.positive_examples.join('\n')} onChange={event => update(item => { item.positive_examples = lines(event.target.value); })} /></label>
        <label>相似但不能命中的问法（每行一个）<textarea rows={4} value={answer.negative_examples.join('\n')} onChange={event => update(item => { item.negative_examples = lines(event.target.value); })} /></label>
        <label>{answer.answer_origin === 'website_verbatim' ? '官网标准回答' : '客户实际收到的标准回答'}<textarea rows={10} maxLength={10000} value={answer.answer_text} onChange={event => update(item => { item.answer_text = event.target.value; if (item.answer_origin === 'website_verbatim') { item.answer_origin = 'operator_approved'; item.source_ref = 'operator.config'; } })} /><small>{answer.answer_text.length} 字，命中后逐字发送。修改官网原话后，来源会自动改为“运营维护”。</small></label>
        <section className="drawer-linked-section"><div><h4>随答案发送的图片</h4><span>{answer.asset_ids.length} 张</span></div>{selectedGroup?.asset_keys.length ? <div className="fixed-answer-assets">{selectedGroup.asset_keys.map(key => { const asset = product.assets.find(item => item.key === key); return <label key={key}><input type="checkbox" checked={answer.asset_ids.includes(key)} onChange={event => update(item => { item.asset_ids = event.target.checked ? [...item.asset_ids, key] : item.asset_ids.filter(id => id !== key); })} />{asset?.preview_url ? <img src={`${API_BASE}${asset.preview_url}`} alt={asset.display_name} /> : <ImageIcon size={24} />}<span>{asset?.display_name ?? key}</span></label>; })}</div> : <p>所选线路资料没有图片。</p>}</section>
        <div className="fixed-answer-source"><span>内容来源</span><strong>{answer.source_ref}</strong></div>
      </div>
      <footer><button className="secondary-button danger-button" onClick={() => { patch(value => { value.fixed_answers = value.fixed_answers.filter(item => item.id !== answer.id); }); onClose(); }}><Trash2 size={15} />删除</button><button className="primary-button" onClick={onClose}><CheckCircle2 size={16} />完成编辑</button></footer>
    </aside>
  </div>;
}

function FactRow({ fact, onChange, onDelete }: { fact: ProductFact; onChange: (field: 'text' | 'source_ref', value: string) => void; onDelete?: () => void }) {
  return <article><div><strong>价格或档期资料</strong>{onDelete ? <button className="icon-button danger-icon" title="删除资料" onClick={onDelete}><Trash2 size={14} /></button> : null}</div><textarea rows={3} maxLength={4000} value={fact.text} onChange={event => onChange('text', event.target.value)} /><label>资料来源<input value={fact.source_ref} maxLength={500} onChange={event => onChange('source_ref', event.target.value)} /></label></article>;
}

function AssetsTab({ routeVariant, assets, busy, onSave, onReplace }: { routeVariant: string; assets: ProductAsset[]; busy: boolean; onSave: (asset: ProductAsset, values: AssetNarrativeDraft) => void; onReplace: (asset: ProductAsset, file: File) => void }) {
  return <div className="route-section-stack"><header className="detail-section-intro"><div><h2>图片与文件素材</h2><p>为素材填写准确说明。PDF 上传或更换后需重新审核批准，批准不会向客户发送消息。</p></div></header><div className="route-material-grid">{assets.map(asset => <MaterialCard key={`${asset.key}:${asset.media_id}`} routeVariant={routeVariant} asset={asset} busy={busy} onSave={values => onSave(asset, values)} onReplace={file => onReplace(asset, file)} />)}</div></div>;
}

type AssetNarrativeDraft = {
  name: string; usage: string; whatItShows: string; featurePoints: string;
  customerValue: string; recommendedCaption: string; avoidClaims: string;
};

function lines(value: string) { return value.split(/\r?\n/).map(item => item.trim()).filter(Boolean); }

function assetDraft(asset: ProductAsset): AssetNarrativeDraft {
  return {
    name: asset.display_name,
    usage: asset.usage,
    whatItShows: asset.what_it_shows ?? '',
    featurePoints: (asset.feature_points ?? []).join('\n'),
    customerValue: asset.customer_value ?? '',
    recommendedCaption: asset.recommended_caption ?? '',
    avoidClaims: (asset.avoid_claims ?? []).join('\n'),
  };
}

function MaterialCard({ routeVariant, asset, busy, onSave, onReplace }: { routeVariant: string; asset: ProductAsset; busy: boolean; onSave: (values: AssetNarrativeDraft) => void; onReplace: (file: File) => void }) {
  const [value, setValue] = useState<AssetNarrativeDraft>(() => assetDraft(asset));
  const queryClient = useQueryClient();
  const [reviewNotes, setReviewNotes] = useState('');
  const isPdf = asset.media_type === 'file' || asset.key === 'china2go-altitude-guide-v1';
  const approve = useMutation({
    mutationFn: () => api(`/automation/route-products/${encodeURIComponent(routeVariant)}/assets/${encodeURIComponent(asset.key)}/approve`, { method: 'POST', body: JSON.stringify({ expected_hash: asset.file_hash, review_notes: reviewNotes }) }),
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ['route-products'] }); setReviewNotes(''); },
  });
  useEffect(() => { setValue(assetDraft(asset)); }, [
    asset.key, asset.media_id, asset.display_name, asset.usage, asset.what_it_shows,
    asset.customer_value, asset.recommended_caption,
    asset.feature_points.join('\n'), asset.avoid_claims.join('\n'),
  ]);
  const saved = assetDraft(asset);
  const dirty = JSON.stringify(value) !== JSON.stringify(saved);
  const patch = (key: keyof AssetNarrativeDraft, next: string) => setValue(current => ({ ...current, [key]: next }));
  const valid = value.name.trim() && value.name.length <= 200 && value.usage.length <= 1000
    && value.whatItShows.trim() && value.whatItShows.length <= 1000
    && lines(value.featurePoints).length <= 8 && value.customerValue.trim() && value.customerValue.length <= 1000
    && value.recommendedCaption.trim() && value.recommendedCaption.length <= 1200
    && lines(value.avoidClaims).length <= 12;
  return <article className="route-material-card"><div className="route-material-preview">{asset.preview_url ? <a href={`${API_BASE}${asset.preview_url}`} target="_blank" rel="noreferrer">{isPdf ? <span><FileCheck2 size={28} />打开 PDF 审阅</span> : <img src={`${API_BASE}${asset.preview_url}`} alt={asset.display_name} />}</a> : <ImageIcon size={32} />}</div><div className="route-material-body"><div><Badge tone={asset.available && asset.live_approved ? 'green' : 'amber'}>{!asset.available ? '缺少文件' : asset.live_approved ? '已批准' : '待审核'}</Badge><code>{asset.key}</code></div><label>素材名称<input value={value.name} maxLength={200} onChange={event => patch('name', event.target.value)} /></label><label>内容主题<input value={value.usage} maxLength={1000} onChange={event => patch('usage', event.target.value)} /></label><label>素材包含什么<textarea rows={2} maxLength={1000} value={value.whatItShows} onChange={event => patch('whatItShows', event.target.value)} /></label><label>可以介绍的特色 <small>每行一项，最多 8 项</small><textarea rows={3} value={value.featurePoints} onChange={event => patch('featurePoints', event.target.value)} /></label><label>对客户的价值<textarea rows={2} maxLength={1000} value={value.customerValue} onChange={event => patch('customerValue', event.target.value)} /></label><label>推荐发送话术<textarea rows={3} maxLength={1200} value={value.recommendedCaption} onChange={event => patch('recommendedCaption', event.target.value)} /></label><label>禁止延伸的说法 <small>每行一项，最多 12 项</small><textarea rows={3} value={value.avoidClaims} onChange={event => patch('avoidClaims', event.target.value)} /></label><small>关联内容：{groupLabels[asset.content_group_key] ?? asset.content_group_key}</small><footer><button className="secondary-button compact" disabled={!dirty || busy || !valid} onClick={() => onSave(value)}><Save size={14} />保存说明</button><label className="primary-button compact"><Upload size={14} />重新上传<input type="file" hidden accept={isPdf ? "application/pdf" : "image/png,image/jpeg,image/webp,image/gif"} disabled={busy} onChange={event => { if (event.target.files?.[0]) onReplace(event.target.files[0]); event.target.value = ''; }} /></label></footer>{isPdf && asset.available && !asset.live_approved ? <div><label>审核记录<textarea value={reviewNotes} onChange={event => setReviewNotes(event.target.value)} maxLength={2000} placeholder="请先打开文件核对内容，再记录审核依据和适用范围。" /></label><button className="secondary-button compact" disabled={busy || approve.isPending || reviewNotes.trim().length < 10 || !asset.file_hash} onClick={() => approve.mutate()}><ShieldCheck size={14} />批准此版本</button>{approve.error ? <p role="alert">{approve.error.message}</p> : null}</div> : null}</div></article>;
}

function VersionsTab({ product }: { product: RouteProduct }) {
  return <div className="route-section-stack"><header className="detail-section-intro"><div><h2>线路版本记录</h2><p>线路版本与全局 AI 接待策略独立发布。</p></div>{product.source.url ? <a className="secondary-button compact" href={product.source.url} target="_blank" rel="noreferrer">原始资料 <ExternalLink size={13} /></a> : null}</header><div className="route-list-table-wrap"><table className="route-list-table"><thead><tr><th>版本</th><th>状态</th><th>变更摘要</th><th>发布时间</th></tr></thead><tbody>{product.versions.map((version, index) => <tr key={`${version.version}:${index}`}><td><strong>{version.version}</strong></td><td>{version.status === 'current' ? <Badge tone="green">当前线上</Badge> : <Badge>历史版本</Badge>}</td><td>{version.summary}</td><td>{formatTime(version.created_at)}</td></tr>)}</tbody></table></div><div className="route-version-note"><FileCheck2 size={16} />发布会生成不可变版本；当前正在执行的客户旅程不会被中途改写。</div></div>;
}
