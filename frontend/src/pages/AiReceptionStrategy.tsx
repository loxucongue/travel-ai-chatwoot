import { useEffect, useMemo, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  AlertTriangle, Bot, CheckCircle2, Clock3, FileClock, LockKeyhole,
  Plus, Save, ShieldCheck, SlidersHorizontal, Trash2, Users, X,
} from 'lucide-react';
import { api, ApiError, DEMO_MODE } from '../api';
import { Badge, EmptyState } from '../components';
import OpeningItemsEditor from './OpeningItemsEditor';
import {
  actionLabels, BusinessRule, clone, ConfigResponse, normalizeConfig,
  OpeningItem, ReceptionConfig,
} from './reception-config';

type StrategyTab = 'base' | 'silence' | 'rules' | 'guards' | 'versions';

const tabItems: { id: StrategyTab; label: string; hint: string; icon: typeof Bot }[] = [
  { id: 'base', label: '基础接待', hint: '目标、语气与留资方式', icon: Bot },
  { id: 'silence', label: '沉默跟进', hint: '触达节奏与发送时段', icon: Clock3 },
  { id: 'rules', label: '全局业务规则', hint: '跨线路通用的处理条件', icon: SlidersHorizontal },
  { id: 'guards', label: '系统保护（只读）', hint: '权限、窗口与防重复', icon: ShieldCheck },
  { id: 'versions', label: '版本记录', hint: '查看策略发布历史', icon: FileClock },
];

const MAX_SILENCE_TOUCHES = 20;
const SUGGESTED_SILENCE_INTERVALS = [
  1, 3, 5, 10, 30, 60, 120, 240, 360, 480,
  600, 720, 840, 960, 1080, 1200, 1260, 1320, 1380, 1440,
];

function nextSilenceInterval(values: number[]) {
  const last = values.at(-1) ?? 0;
  return SUGGESTED_SILENCE_INTERVALS.find(value => value > last)
    ?? Math.min(1440, last + 1);
}

function Switch({ checked, onChange, disabled = false }: { checked: boolean; onChange: (value: boolean) => void; disabled?: boolean }) {
  return <button type="button" className={`strategy-switch ${checked ? 'on' : ''}`} onClick={() => !disabled && onChange(!checked)} disabled={disabled} aria-pressed={checked}><span /></button>;
}

function formatTime(value?: string | null) {
  if (!value) return '暂无记录';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false });
}

function newRule(): BusinessRule {
  return {
    id: `operator_rule_${Date.now()}`,
    name: '新业务规则',
    enabled: true,
    condition: '',
    action: 'continue_ai',
    guidance: '',
    system_key: null,
  };
}

export default function AiReceptionStrategy() {
  const queryClient = useQueryClient();
  const configQuery = useQuery({
    queryKey: ['reception-config'],
    queryFn: () => api<ConfigResponse>('/automation/reception-config'),
  });
  const globalSending = useQuery({
    queryKey: ['global-message-sending'],
    queryFn: () => api<{ enabled: boolean; effective_enabled: boolean }>('/settings/global-message-sending'),
    enabled: !DEMO_MODE,
  });
  const [tab, setTab] = useState<StrategyTab>('base');
  const [draft, setDraft] = useState<ReceptionConfig | null>(null);
  const [saved, setSaved] = useState('');
  const [notice, setNotice] = useState('');
  const uploads = useRef(new Set<string>());
  const [uploading, setUploading] = useState<string[]>([]);
  const [uploadErrors, setUploadErrors] = useState<Record<string, string>>({});
  const [previewStatus, setPreviewStatus] = useState<Record<string, 'ready' | 'error'>>({});

  useEffect(() => {
    if (configQuery.data?.config && !draft) {
      const value = normalizeConfig(configQuery.data.config);
      setDraft(value);
      setSaved(JSON.stringify(value));
    }
  }, [configQuery.data, draft]);

  const dirty = !!draft && JSON.stringify(draft) !== saved;
  const publish = useMutation({
    mutationFn: () => {
      if (uploads.current.size) throw new Error('请等待附件上传完成。');
      return api<{ config: ReceptionConfig }>('/automation/reception-config', {
      method: 'PUT',
      body: JSON.stringify(draft),
      });
    },
    onSuccess: async data => {
      const value = normalizeConfig(data.config);
      setDraft(value);
      setSaved(JSON.stringify(value));
      setNotice('全局 AI 接待策略已发布。');
      await queryClient.invalidateQueries({ queryKey: ['reception-config'] });
    },
  });

  function patch(updater: (value: ReceptionConfig) => void) {
    setDraft(current => {
      if (!current) return current;
      const value = clone(current);
      updater(value);
      if (JSON.stringify(value.reply.opening_items) !== JSON.stringify(current.reply.opening_items)) {
        value.reply.opening_messages = value.reply.opening_items.filter(item => item.content_type === 'text').map(item => item.content);
        value.reply.opening_message = value.reply.opening_messages[0] ?? '';
      }
      return value;
    });
    setNotice('');
  }

  async function uploadOpening(item: OpeningItem, file: File) {
    if (uploads.current.has(item.key) || publish.isPending) return;
    const allowed = item.content_type === 'image'
      ? ['image/png', 'image/jpeg', 'image/webp', 'image/gif']
      : ['video/mp4', 'video/webm'];
    const extensions = item.content_type === 'image' ? /\.(png|jpe?g|webp|gif)$/i : /\.(mp4|webm)$/i;
    const error = !file.size || file.size > 20 * 1024 * 1024
      ? '文件应大于 0 且不超过 20 MB'
      : !extensions.test(file.name) || (file.type && !allowed.includes(file.type))
        ? '图片仅支持 PNG/JPEG/WebP/GIF，视频仅支持 MP4/WebM' : '';
    setUploadErrors(current => ({ ...current, [item.key]: error }));
    if (error) return;
    uploads.current.add(item.key);
    setUploading([...uploads.current]);
    try {
      const body = new FormData();
      body.append('file', file);
      const result = await api<{ id: number; name: string; media_hash?: string }>('/media', { method: 'POST', body });
      if (!Number.isInteger(result.id) || result.id <= 0) throw new Error('上传响应缺少有效附件 ID，请重试。');
      patch(value => {
        const target = value.reply.opening_items.find(entry => entry.key === item.key);
        if (!target || target.content_type !== item.content_type) return;
        Object.assign(target, { media_id: result.id, media_name: result.name || file.name, media_hash: result.media_hash });
      });
    } catch (error) {
      setUploadErrors(current => ({ ...current, [item.key]: error instanceof Error ? error.message : '上传失败，请重试' }));
    } finally {
      uploads.current.delete(item.key);
      setUploading([...uploads.current]);
    }
  }

  const validationError = useMemo(() => {
    if (!draft) return '';
    if (!draft.reply.goal.trim()) return '请填写 AI 接待目标。';
    if (draft.reply.goal.length > 160) return 'AI 接待目标最多 160 字。';
    if (draft.reply.tone_guidance.length > 1200) return '自定义表达语气最多 1200 字。';
    const items = draft.reply.opening_items;
    if (!items.length || items.length > 10) return '开场消息需包含 1 至 10 条。';
    if (items.some(item => !/^[a-zA-Z0-9_-]{1,80}$/.test(item.key)) || new Set(items.map(item => item.key)).size !== items.length) return '开场消息标识无效或重复，请重新读取配置。';
    if (items.some(item => !['text', 'image', 'video'].includes(item.content_type))) return '开场消息类型不支持。';
    if (items.some(item => Array.from(item.content.trim()).length > draft.reply.max_characters || (item.content_type === 'text' ? !item.content.trim() : !Number.isInteger(item.media_id) || Number(item.media_id) <= 0))) return '请填写开场文字或上传附件，文字和附件说明不得超过回复字数上限。';
    const media = items.filter(item => item.content_type !== 'text');
    if (new Set(media.map(item => item.media_id)).size !== media.length || new Set(media.filter(item => item.media_hash).map(item => item.media_hash)).size !== media.filter(item => item.media_hash).length) return '开场消息不能重复使用同一附件。';
    if (media.some(item => previewStatus[`${item.content_type}:${item.media_id}`] === 'error')) return '附件预览失败，请检查或重新上传后再发布。';
    if (media.some(item => item.content_type === 'video' && previewStatus[`video:${item.media_id}`] !== 'ready')) return '请等待视频预览就绪后再发布。';
    if (!Number.isInteger(draft.reply.opening_interval_seconds) || draft.reply.opening_interval_seconds < 1 || draft.reply.opening_interval_seconds > 30) return '开场消息间隔需为1至30秒。';
    if (draft.reply.custom_guidance.length > 1200) return '其他全局说明最多 1200 字。';
    if (draft.reply.max_characters < 80 || draft.reply.max_characters > 200) return '单次回复上限必须在 80–200 字之间。';
    if (draft.reply.max_images_per_turn < 0 || draft.reply.max_images_per_turn > 2) return '单次图片数量必须在 0–2 张之间。';
    if (draft.lead_capture.enabled && !draft.lead_capture.channels.length) return '开启留资后，至少选择一种联系方式。';
    if (!draft.silence.intervals_minutes.length) return '请至少保留一个沉默跟进时间。';
    if (draft.silence.intervals_minutes.length > MAX_SILENCE_TOUCHES) return `沉默跟进最多允许 ${MAX_SILENCE_TOUCHES} 个时间节点。`;
    if (draft.silence.intervals_minutes.some(value => !Number.isInteger(value) || value < 1 || value > 1440)) return '每个沉默间隔必须是 1–1440 的整数分钟。';
    if (draft.silence.intervals_minutes.some((value, index, values) => index > 0 && value <= values[index - 1])) return '沉默跟进时间必须按从小到大排列，且不能重复。';
    if (draft.silence.max_proactive_messages_per_day > draft.silence.intervals_minutes.length) return '每天最多主动消息不能超过时间节点数量。';
    if (draft.silence.max_proactive_messages_per_day < 1) return '每天最多主动消息至少为 1 条。';
    if (draft.handoff.large_group_minimum < 2 || draft.handoff.large_group_minimum > 100) return '大团人数必须在 2–100 人之间。';
    if (draft.business_rules.length > 30) return '自定义业务规则最多 30 条。';
    const incompleteRule = draft.business_rules.find(rule => !rule.name.trim() || !rule.condition.trim());
    if (incompleteRule) return '业务规则名称和触发条件不能为空。';
    const oversizedRule = draft.business_rules.find(rule => rule.name.length > 80 || rule.condition.length > 500 || rule.guidance.length > 500);
    if (oversizedRule) return '业务规则名称最多 80 字，触发条件和处理说明最多 500 字。';
    return '';
  }, [draft, previewStatus]);

  const error = configQuery.error || publish.error || globalSending.error;
  if (configQuery.isLoading) return <div className="page-content ai-strategy-page"><section className="panel"><EmptyState type="loading" title="正在读取 AI 接待策略" description="" /></section></div>;
  if (!draft || !configQuery.data) return <div className="page-content ai-strategy-page"><section className="panel"><EmptyState type="error" title="策略读取失败" description={(error as Error)?.message ?? '无法读取策略'} /></section></div>;

  const version = configQuery.data.version;
  return <div className="page-content ai-strategy-page">
    {notice ? <div className="config-notice"><CheckCircle2 size={16} /><span>{notice}</span><button onClick={() => setNotice('')}><X size={14} /></button></div> : null}
    {error ? <div className="config-error"><AlertTriangle size={16} />{(error as ApiError).message}</div> : null}

    <header className="strategy-page-head">
      <div>
        <span className="eyebrow">AI RECEPTION POLICY</span>
        <h1>AI 接待策略</h1>
        <p>全部线路共用的低频配置，由运营主管或管理员维护。</p>
      </div>
      <div className="strategy-version-tags"><Badge tone="blue">全局配置</Badge><Badge>{version?.label ?? '当前版本'}</Badge><Badge tone="green">已发布</Badge></div>
    </header>

    <div className="strategy-scope-note"><AlertTriangle size={18} /><div><strong>修改后将影响全部启用线路</strong><span>线路价格、行程、事实和图片请在线路管理中维护。这里不保存任何线路专属内容。</span></div></div>

    <div className="strategy-layout">
      <nav className="strategy-subnav panel" aria-label="AI 接待策略设置">
        {tabItems.map(item => { const Icon = item.icon; return <button key={item.id} className={tab === item.id ? 'active' : ''} onClick={() => setTab(item.id)}><Icon size={17} /><span><strong>{item.label}</strong><small>{item.hint}</small></span></button>; })}
      </nav>

      <section className="strategy-main panel">
        {tab === 'base' ? <BaseStrategy config={draft} patch={patch} openingEditor={<OpeningItemsEditor config={draft} patch={patch} uploading={uploading} errors={uploadErrors} disabled={publish.isPending} upload={uploadOpening} previewChanged={(key, status) => setPreviewStatus(current => current[key] === status ? current : { ...current, [key]: status })} clearError={key => setUploadErrors(current => ({ ...current, [key]: '' }))} />} /> : null}
        {tab === 'silence' ? <SilenceStrategy config={draft} patch={patch} /> : null}
        {tab === 'rules' ? <RulesStrategy config={draft} patch={patch} /> : null}
        {tab === 'guards' ? <SystemGuards hardGuards={configQuery.data.hard_guards} sendingEnabled={globalSending.data?.effective_enabled ?? false} /> : null}
        {tab === 'versions' ? <VersionHistory response={configQuery.data} /> : null}
      </section>
    </div>

    {dirty || uploading.length ? <footer className="strategy-save-bar"><div>{uploading.length ? <span role="status">附件上传中，请稍候</span> : validationError ? <span className="save-error"><AlertTriangle size={15} />{validationError}</span> : <span>存在尚未发布的全局策略修改</span>}</div><button className="secondary-button" disabled={!!uploading.length || publish.isPending} onClick={() => { const value = normalizeConfig(configQuery.data.config); setDraft(value); setSaved(JSON.stringify(value)); setUploadErrors({}); }}>放弃修改</button><button className="primary-button" disabled={!!validationError || !!uploading.length || publish.isPending} onClick={() => publish.mutate()}><Save size={15} />{publish.isPending ? '发布中…' : '发布策略'}</button></footer> : null}
  </div>;
}

function SectionHead({ title, description }: { title: string; description: string }) {
  return <header className="strategy-section-head"><h2>{title}</h2><p>{description}</p></header>;
}

function BaseStrategy({ config, patch, openingEditor }: { config: ReceptionConfig; patch: (updater: (value: ReceptionConfig) => void) => void; openingEditor: React.ReactNode }) {
  return <div className="strategy-section-stack">
    <SectionHead title="基础接待" description="定义所有线路共同使用的回复目标、表达方式和留资原则。" />
    {openingEditor}
    <section className="reply-expression-editor">
      <h3>AI 回覆語氣 · 即時與沉默跟進</h3>
      <div className="strategy-field-grid">
        <label className="full">台灣顧問語氣<div className="strategy-segments">{([['friendly_professional', '親切柔和'], ['concise', '簡短有禮'], ['warm', '溫柔可親']] as const).map(([value, label]) => <button type="button" aria-pressed={config.reply.tone === value} key={value} className={config.reply.tone === value ? 'active' : ''} onClick={() => patch(draft => { draft.reply.tone = value; })}>{label}</button>)}</div></label>
      </div>
      <label className="block-field">補充語氣要求<textarea rows={5} value={config.reply.tone_guidance} maxLength={1200} placeholder="親切、輕柔，像在 LINE 聊行程；可以帶一點可愛語尾，不要每句重複。" onChange={event => patch(value => { value.reply.tone_guidance = event.target.value; })} /><small>{config.reply.tone_guidance.length}/1200 字</small></label>
      <div className="strategy-field-grid">
        <label>單則文字上限<div className="number-suffix"><input type="number" min={80} max={200} value={config.reply.max_characters} onChange={event => patch(value => { value.reply.max_characters = Number(event.target.value); })} /><span>字</span></div></label>
        <label>AI 單輪圖片上限<div className="number-suffix"><input type="number" min={0} max={2} value={config.reply.max_images_per_turn} onChange={event => patch(value => { value.reply.max_images_per_turn = Number(event.target.value); })} /><span>張</span></div></label>
      </div>
      <details><summary>接待目標與其他表達要求</summary><div className="reply-expression-extra">
        <label className="block-field">接待目標<textarea rows={3} value={config.reply.goal} maxLength={160} onChange={event => patch(value => { value.reply.goal = event.target.value; })} /><small>{config.reply.goal.length}/160 字</small></label>
        <label className="block-field">其他表達要求<textarea rows={3} value={config.reply.custom_guidance} maxLength={1200} onChange={event => patch(value => { value.reply.custom_guidance = event.target.value; })} /><small>{config.reply.custom_guidance.length}/1200 字</small></label>
      </div></details>
    </section>

    <div className="strategy-card">
      <div className="strategy-card-title"><div><h3>联系方式获取</h3><p>AI 先回答客户问题，信息和意向足够后，再自然索取一种联系方式。</p></div><Switch checked={config.lead_capture.enabled} onChange={checked => patch(value => { value.lead_capture.enabled = checked; })} /></div>
      <div className="strategy-check-grid">{(['LINE', '微信', '电话', 'Email'] as const).map(channel => <label key={channel}><input type="checkbox" checked={config.lead_capture.channels.includes(channel)} disabled={!config.lead_capture.enabled} onChange={event => patch(value => { value.lead_capture.channels = event.target.checked ? [...value.lead_capture.channels, channel] : value.lead_capture.channels.filter(item => item !== channel); })} /><span>{channel}</span></label>)}</div>
      <div className="strategy-inline-options">
        <label><input type="checkbox" checked={config.lead_capture.require_supported_route} onChange={event => patch(value => { value.lead_capture.require_supported_route = event.target.checked; })} /> 已确认平台支持的线路</label>
        <label><input type="checkbox" checked={config.lead_capture.answer_before_asking} onChange={event => patch(value => { value.lead_capture.answer_before_asking = event.target.checked; })} /> 必须先回答当前问题</label>
        <label><input type="checkbox" checked={config.lead_capture.require_party_size} onChange={event => patch(value => { value.lead_capture.require_party_size = event.target.checked; })} /> 优先了解同行人数（非硬门槛）</label>
        <label><input type="checkbox" checked={config.lead_capture.require_departure_window} onChange={event => patch(value => { value.lead_capture.require_departure_window = event.target.checked; })} /> 优先了解出发时间（不确定时不追问）</label>
      </div>
    </div>

    <div className="strategy-card">
      <h3>线路协同</h3>
      <div className="strategy-toggle-list">
        <label><span><strong>允许客户切换线路</strong><small>客户改问另一条已启用线路时，由 AI 重新判断并切换。</small></span><Switch checked={config.routing.allow_route_switch} onChange={checked => patch(value => { value.routing.allow_route_switch = checked; })} /></label>
        <label><span><strong>切换时保留客户档案</strong><small>人数、出发时间和预算等继续沿用，不要求客户重复提供。</small></span><Switch checked={config.routing.preserve_profile_on_switch} onChange={checked => patch(value => { value.routing.preserve_profile_on_switch = checked; })} /></label>
      </div>
      <label className="block-field">客户咨询资料外线路<select value={config.routing.outside_catalog_action} onChange={event => patch(value => { value.routing.outside_catalog_action = event.target.value as ReceptionConfig['routing']['outside_catalog_action']; })}><option value="recommend_supported_routes">说明范围并推荐当前已启用线路</option><option value="explain_boundary_only">只说明资料边界，不主动推荐</option></select></label>
    </div>
  </div>;
}

function SilenceStrategy({ config, patch }: { config: ReceptionConfig; patch: (updater: (value: ReceptionConfig) => void) => void }) {
  const intervals = config.silence.intervals_minutes;
  const canAdd = config.silence.enabled
    && intervals.length < MAX_SILENCE_TOUCHES
    && (intervals.at(-1) ?? 0) < 1440;
  function addInterval() {
    patch(value => {
      const currentLength = value.silence.intervals_minutes.length;
      value.silence.intervals_minutes.push(nextSilenceInterval(value.silence.intervals_minutes));
      if (value.silence.max_proactive_messages_per_day === currentLength) {
        value.silence.max_proactive_messages_per_day = currentLength + 1;
      }
    });
  }
  function removeInterval(index: number) {
    patch(value => {
      value.silence.intervals_minutes.splice(index, 1);
      value.silence.max_proactive_messages_per_day = Math.min(
        value.silence.max_proactive_messages_per_day,
        value.silence.intervals_minutes.length,
      );
    });
  }
  return <div className="strategy-section-stack">
    <SectionHead title="沉默跟进" description="间隔从客户最后一次发言或上一条成功触达开始计算，与固定话术和具体素材无绑定。" />
    <div className="strategy-card">
      <h3>V2 普通沉默跟进</h3>
      <p>第一项从本轮回复交付后计算，后续从上一次成功跟进计算。考虑中或约定联系另行处理；到期仍须有新价值且渠道允许发送。</p>
      {(config.silence.v2_intervals_minutes ?? [1, 120]).map((minutes, index) => <label className="block-field" key={index}>第 {index + 1} 次间隔（分钟）<input type="number" min={1} max={1434} value={minutes} onChange={event => patch(value => {
        const intervals = [...(value.silence.v2_intervals_minutes ?? [1, 120])];
        intervals[index] = Number(event.target.value);
        value.silence.v2_intervals_minutes = intervals;
      })} /></label>)}
      <p>间隔总和必须小于 23 小时 55 分。下方时间线用于 V1，发送时段及总开关仍共同生效。</p>
    </div>
    <div className="strategy-card">
      <div className="strategy-card-title"><div><h3>客户沉默后继续推进</h3><p>无安全阻断时，每个到期节点都必须提供新的有效价值。</p></div><Switch checked={config.silence.enabled} onChange={checked => patch(value => { value.silence.enabled = checked; })} /></div>
      <div className="silence-timeline-editor">{intervals.map((minutes, index) => <div key={index} className="silence-time-item"><span>第 {index + 1} 次</span><div className="silence-time-control"><label><input type="number" min={1} max={1440} value={minutes} disabled={!config.silence.enabled} aria-label={`第 ${index + 1} 次沉默跟进间隔`} onChange={event => patch(value => { value.silence.intervals_minutes[index] = Number(event.target.value); })} /><em>分钟后</em></label><button type="button" className="icon-button danger-icon silence-delete" title="删除时间节点" aria-label={`删除第 ${index + 1} 次时间节点`} disabled={!config.silence.enabled || intervals.length === 1} onClick={() => removeInterval(index)}><Trash2 size={14} /></button></div>{index < intervals.length - 1 ? <i>→</i> : null}</div>)}<button type="button" className="secondary-button compact silence-add" disabled={!canAdd} onClick={addInterval}><Plus size={14} />添加时间节点</button></div>
      <p className="silence-timeline-help">每个时间表示距上一次成功触达的间隔；至少 1 个，最多 {MAX_SILENCE_TOUCHES} 个。新发布的设置只应用于之后建立的客户旅程。</p>
      <div className="strategy-field-grid compact-grid">
        <label>每天最多主动消息<div className="number-suffix"><input type="number" min={1} max={20} value={config.silence.max_proactive_messages_per_day} onChange={event => patch(value => { value.silence.max_proactive_messages_per_day = Number(event.target.value); })} /><span>条</span></div></label>
        <label>允许发送开始时间<input type="time" value={config.silence.active_start} onChange={event => patch(value => { value.silence.active_start = event.target.value; })} /></label>
        <label>允许发送结束时间<input type="time" value={config.silence.active_end} onChange={event => patch(value => { value.silence.active_end = event.target.value; })} /></label>
      </div>
      <div className="strategy-info-row"><CheckCircle2 size={16} /><span>客户一旦回复，当前沉默轮次立即取消；AI 回答完成后，再按最新阶段重新开始计时。</span></div>
    </div>
    <div className="strategy-card readonly-card">
      <h3>固定执行方式</h3>
      <div className="readonly-rule-grid">
        <div><strong>先筛选再生成</strong><span>代码先确认还有相关的新内容；没有新价值时安全跳过本次触达。</span></div>
        <div><strong>单节点最多修复 1 次</strong><span>只修复当前节点的 JSON 结构；业务动作不会交给模型反复修改。</span></div>
        <div><strong>完成后停止</strong><span>最后一次触达完成后结束，不无限循环打扰客户。</span></div>
      </div>
    </div>
  </div>;
}

function RulesStrategy({ config, patch }: { config: ReceptionConfig; patch: (updater: (value: ReceptionConfig) => void) => void }) {
  const editable = config.business_rules.filter(rule => !rule.system_key);
  return <div className="strategy-section-stack">
    <SectionHead title="全局业务规则" description="维护全部线路共用的业务条件。安全保护规则不会放在这里编辑。" />
    <div className="strategy-card">
      <div className="strategy-card-title"><div><h3>大团交给人工</h3><p>只有客户明确给出人数并达到阈值时才触发。</p></div><Switch checked={config.handoff.large_group_enabled} onChange={checked => patch(value => { value.handoff.large_group_enabled = checked; })} /></div>
      <label className="compact-number-field">人数达到 <input type="number" min={2} max={100} value={config.handoff.large_group_minimum} disabled={!config.handoff.large_group_enabled} onChange={event => patch(value => { value.handoff.large_group_minimum = Number(event.target.value); })} /> 人，自动转人工</label>
    </div>
    <div className="strategy-card">
      <div className="strategy-card-title"><div><h3>自定义规则</h3><p>规则描述会交给模型判断；请写清触发条件和期望动作，不使用关键词列表。</p></div><button className="secondary-button compact" disabled={config.business_rules.length >= 30} onClick={() => patch(value => { value.business_rules.push(newRule()); })}><Plus size={14} />新增规则</button></div>
      {!editable.length ? <EmptyState title="暂无自定义规则" description="只有跨线路共用的业务逻辑才需要放在这里。" /> : <div className="business-rule-list">{editable.map(rule => {
        const index = config.business_rules.findIndex(item => item.id === rule.id);
        return <article key={rule.id}>
          <label className="block-field">匹配方式<select value={rule.trigger ?? 'semantic'} onChange={event => patch(value => { value.business_rules[index].trigger = event.target.value as BusinessRule['trigger']; })}><option value="semantic">自定义语义条件</option><option value="outside_catalog">客户所需线路不在产品目录内</option></select></label>
          <div className="rule-row-head"><Switch checked={rule.enabled} onChange={checked => patch(value => { value.business_rules[index].enabled = checked; })} /><input value={rule.name} maxLength={80} onChange={event => patch(value => { value.business_rules[index].name = event.target.value; })} /><button className="icon-button danger-icon" title="删除规则" onClick={() => patch(value => { value.business_rules.splice(index, 1); })}><Trash2 size={15} /></button></div>
          <div className="rule-field-grid"><label>触发条件<textarea rows={2} value={rule.condition} maxLength={500} onChange={event => patch(value => { value.business_rules[index].condition = event.target.value; })} /></label><label>触发后动作<select value={rule.action} onChange={event => patch(value => { value.business_rules[index].action = event.target.value as BusinessRule['action']; })}>{Object.entries(actionLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label><label className="full">处理说明<textarea rows={2} value={rule.guidance} maxLength={500} placeholder="告诉 AI 触发后应该如何表达和继续接待。" onChange={event => patch(value => { value.business_rules[index].guidance = event.target.value; })} /></label></div>
        </article>;
      })}</div>}
    </div>
  </div>;
}

function SystemGuards({ hardGuards, sendingEnabled }: { hardGuards: Record<string, boolean | string>; sendingEnabled: boolean }) {
  const guards = [
    { title: '全局消息发送总开关', value: sendingEnabled ? '已开启' : '已关闭', good: !sendingEnabled, description: '关闭时所有 AI 回复和沉默触达都禁止真实发送。开关位于页面顶部。' },
    { title: 'AI 接管范围', value: hardGuards.require_ai_label ? '需要 ai 标签' : '全部符合条件的会话', good: true, description: `当前范围：${String(hardGuards.rollout_scope ?? '未设置')}` },
    { title: '渠道回复窗口', value: hardGuards.respect_channel_window ? '强制检查' : '未开启', good: !!hardGuards.respect_channel_window, description: 'Facebook 等渠道关闭回复窗口后，系统不会强行发送。' },
    { title: '人工回复优先', value: hardGuards.stop_on_human_reply ? '强制停止' : '未开启', good: !!hardGuards.stop_on_human_reply, description: '真人接管或抢先回复后，废弃尚未发送的 AI 结果。' },
    { title: '消息幂等', value: hardGuards.deduplicate_messages ? '已开启' : '未开启', good: !!hardGuards.deduplicate_messages, description: '同一客户事件和未知发送状态不会被重复提交。' },
    { title: '素材去重', value: hardGuards.deduplicate_materials ? '已开启' : '未开启', good: !!hardGuards.deduplicate_materials, description: '已经提供过的同一素材不会重复发送。' },
    { title: '演练环境出站', value: hardGuards.evaluation_outbound ? '允许' : '禁止', good: !hardGuards.evaluation_outbound, description: 'AI 演练和回放只能模拟，不能写入 Chatwoot。' },
  ];
  return <div className="strategy-section-stack">
    <SectionHead title="系统保护（只读）" description="这些是防止误发、重复发送和人机冲突的硬性约束，不能作为业务配置关闭。" />
    <div className="guard-grid">{guards.map(item => <article key={item.title}><span className={item.good ? 'guard-icon good' : 'guard-icon warn'}>{item.good ? <ShieldCheck size={18} /> : <AlertTriangle size={18} />}</span><div><strong>{item.title}</strong><p>{item.description}</p></div><Badge tone={item.good ? 'green' : 'amber'}>{item.value}</Badge></article>)}</div>
    <div className="strategy-card readonly-card"><h3>固定转人工保护</h3><div className="readonly-rule-grid"><div><strong>客户已提供有效联系方式</strong><span>确认收到后结束 AI，并交给顾问继续。</span></div><div><strong>客户明确要求真人</strong><span>不继续营销，不与人工同时回复。</span></div><div><strong>投诉、退款或合同争议</strong><span>不由 AI 承诺处理结果。</span></div><div><strong>必须查看附件内容</strong><span>当前无法可靠理解附件时交由人工检查。</span></div></div></div>
  </div>;
}

function VersionHistory({ response }: { response: ConfigResponse }) {
  return <div className="strategy-section-stack">
    <SectionHead title="版本记录" description="每次发布都会记录操作时间和影响范围，正在执行的旧旅程不会被中途改写。" />
    <div className="strategy-version-summary"><FileClock size={20} /><div><strong>{response.version?.label ?? '当前版本'} · 已发布</strong><span>提示词 {response.version?.prompt_version ?? '—'} · 校验器 {response.version?.validator_version ?? '—'}</span><small>最近发布：{formatTime(response.version?.published_at)}</small></div></div>
    <div className="route-list-table-wrap"><table className="route-list-table"><thead><tr><th>记录</th><th>变更摘要</th><th>操作人</th><th>发布时间</th></tr></thead><tbody>{response.history?.length ? response.history.map(row => <tr key={row.id}><td><strong>{row.label}</strong></td><td>{row.summary}</td><td>{row.user_id ? `用户 #${row.user_id}` : '系统初始化'}</td><td>{formatTime(row.created_at)}</td></tr>) : <tr><td colSpan={4}>暂无策略变更记录</td></tr>}</tbody></table></div>
  </div>;
}
