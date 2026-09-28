import { useEffect, useMemo, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  Bot, CheckCircle2, Clock3,
  Plus, Save, SlidersHorizontal, Trash2, Users, X,
} from 'lucide-react';
import { api, ApiError } from '../api';
import { Badge, EmptyState } from '../components';
import OpeningItemsEditor from './OpeningItemsEditor';
import {
  configurationChanges, clone, ConfigResponse, normalizeConfig,
  OpeningItem, ReceptionConfig,
} from './reception-config';

type StrategyTab = 'base' | 'scripts' | 'lead' | 'silence';
const tabItems: { id: StrategyTab; label: string; icon: typeof Bot }[] = [
  {id: 'base', label: '开场与语气', icon: Bot},
  {id: 'scripts', label: '通用话术', icon: SlidersHorizontal},
  {id: 'lead', label: '留资与交接', icon: Users},
  {id: 'silence', label: '沉默跟进', icon: Clock3},
];

function Switch({ checked, onChange, disabled = false }: { checked: boolean; onChange: (value: boolean) => void; disabled?: boolean }) {
  return <button type="button" className={`strategy-switch ${checked ? 'on' : ''}`} onClick={() => !disabled && onChange(!checked)} disabled={disabled} aria-pressed={checked}><span /></button>;
}

export default function AiReceptionStrategy() {
  const queryClient = useQueryClient();
  const configQuery = useQuery({
    queryKey: ['reception-config'],
    queryFn: () => api<ConfigResponse>('/automation/reception-config'),
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
    if (configQuery.data?.config && (!draft || JSON.stringify(draft) === saved)) {
      const value = normalizeConfig(configQuery.data.config);
      if (JSON.stringify(value) === JSON.stringify(draft)) return;
      setDraft(value);
      setSaved(JSON.stringify(value));
    }
  }, [configQuery.data, draft]);

  const dirty = !!draft && JSON.stringify(draft) !== saved;
  const publish = useMutation({
    mutationFn: () => {
      if (uploads.current.size) throw new Error('请等待附件上传完成。');
      return api<{ config: ReceptionConfig }>('/automation/reception-config', {
      method: 'PATCH',
      body: JSON.stringify(configurationChanges(JSON.parse(saved), draft!)),
      });
    },
    onSuccess: async data => {
      const value = normalizeConfig(data.config);
      setDraft(value);
      setSaved(JSON.stringify(value));
      setNotice('已保存');
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
    if (items.some(item => Array.from(item.content.trim()).length > draft.reply.opening_character_limit || (item.content_type === 'text' ? !item.content.trim() : !Number.isInteger(item.media_id) || Number(item.media_id) <= 0))) return '请填写开场文字或上传附件，文字和附件说明不得超过回复字数上限。';
    const media = items.filter(item => item.content_type !== 'text');
    if (new Set(media.map(item => item.media_id)).size !== media.length || new Set(media.filter(item => item.media_hash).map(item => item.media_hash)).size !== media.filter(item => item.media_hash).length) return '开场消息不能重复使用同一附件。';
    if (media.some(item => previewStatus[`${item.content_type}:${item.media_id}`] === 'error')) return '附件预览失败，请检查或重新上传后再发布。';
    if (media.some(item => item.content_type === 'video' && previewStatus[`video:${item.media_id}`] !== 'ready')) return '请等待视频预览就绪后再发布。';
    if (!Number.isInteger(draft.reply.opening_interval_seconds) || draft.reply.opening_interval_seconds < 1 || draft.reply.opening_interval_seconds > 30) return '开场消息间隔需为1至30秒。';
    if (draft.reply.custom_guidance.length > 40000) return '其他全局说明最多 40000 字。';
    if (draft.reply.opening_character_limit < 80 || draft.reply.opening_character_limit > 200) return '单次回复上限必须在 80–200 字之间。';
    if (draft.lead_capture.enabled && !draft.lead_capture.channels.length) return '开启留资后，至少选择一种联系方式。';
    const intervals = draft.silence.intervals_minutes ?? [];
    if (!intervals.length || intervals.some(value => !Number.isInteger(value) || value < 1) || intervals.reduce((sum, value) => sum + value, 0) >= 1435) return '请填写有效的跟进间隔，总时长应少于 1435 分钟。';
    if (draft.common_scripts.some(item => !item.name.trim() || !item.scenario.trim() || !item.text.trim())) return '请补全话术名称、适用场景和正文。';
    return '';
  }, [draft, previewStatus]);

  const error = configQuery.error || publish.error;
  if (configQuery.isLoading) return <div className="page-content ai-strategy-page"><section className="panel"><EmptyState type="loading" title="正在读取 AI 接待策略" description="" /></section></div>;
  if (!draft || !configQuery.data) return <div className="page-content ai-strategy-page"><section className="panel"><EmptyState type="error" title="策略读取失败" description={(error as Error)?.message ?? '无法读取策略'} /></section></div>;

  return <div className="page-content ai-strategy-page">
    {notice ? <div className="config-notice"><CheckCircle2 size={16} />{notice}<button onClick={() => setNotice('')}><X size={14} /></button></div> : null}
    {error ? <div className="config-error" role="alert">{(error as ApiError).message}</div> : null}
    <header className="strategy-page-head"><h1>公共接待</h1><Badge tone="blue">全部线路共用</Badge></header>
    <div className="strategy-layout">
      <nav className="strategy-subnav panel" aria-label="公共接待设置">{tabItems.map(item => { const Icon = item.icon; return <button key={item.id} className={tab === item.id ? 'active' : ''} onClick={() => setTab(item.id)}><Icon size={17} /><span><strong>{item.label}</strong></span></button>; })}</nav>
      <section className="strategy-main panel">
        {tab === 'base' && <div className="strategy-section-stack">
          <OpeningItemsEditor config={draft} patch={patch} uploading={uploading} errors={uploadErrors} disabled={publish.isPending} upload={uploadOpening} previewChanged={(key, status) => setPreviewStatus(current => current[key] === status ? current : {...current, [key]: status})} clearError={key => setUploadErrors(current => ({...current, [key]: ''}))} />
          <section className="reply-expression-editor"><h3>回复语气</h3>
            <div className="strategy-segments">{([['friendly_professional', '亲切柔和'], ['concise', '简短有礼'], ['warm', '温柔可亲']] as const).map(([value,label]) => <button key={value} className={draft.reply.tone === value ? 'active' : ''} onClick={() => patch(config => { config.reply.tone=value; })}>{label}</button>)}</div>
            <label className="block-field">语气要求<textarea rows={4} value={draft.reply.tone_guidance} onChange={event => patch(config => {config.reply.tone_guidance=event.target.value;})} /></label>
            <label className="block-field">接待目标<textarea rows={2} value={draft.reply.goal} onChange={event => patch(config => {config.reply.goal=event.target.value;})} /></label>
            <label className="block-field">其他接待说明<textarea rows={4} value={draft.reply.custom_guidance} onChange={event => patch(config => {config.reply.custom_guidance=event.target.value;})} /></label>
          </section>
        </div>}
        {tab === 'scripts' && <div className="strategy-section-stack"><div className="strategy-card-title"><h3>通用话术</h3><button className="secondary-button compact" disabled={draft.common_scripts.length >= 100} onClick={() => patch(config => {config.common_scripts.push({id: crypto.randomUUID(), name:'', scenario:'', text:'', enabled:true});})}><Plus size={14} />新增话术</button></div>
          {draft.common_scripts.length === 0 && <EmptyState title="暂无通用话术" description="" />}
          {draft.common_scripts.map((item,index) => <article className="strategy-card" key={item.id}>
            <div className="strategy-card-title"><strong>话术 {index+1}</strong><Switch checked={item.enabled} onChange={enabled => patch(config => {config.common_scripts[index].enabled=enabled;})} /><button className="icon-button" aria-label={`删除话术 ${index+1}`} onClick={() => patch(config => {config.common_scripts.splice(index,1);})}><Trash2 size={16} /></button></div>
            <label className="block-field">名称<input value={item.name} onChange={event => patch(config => {config.common_scripts[index].name=event.target.value;})} /></label>
            <label className="block-field">适用场景<textarea rows={2} value={item.scenario} onChange={event => patch(config => {config.common_scripts[index].scenario=event.target.value;})} /></label>
            <label className="block-field">话术原文<textarea rows={5} value={item.text} onChange={event => patch(config => {config.common_scripts[index].text=event.target.value;})} /></label>
          </article>)}
        </div>}
        {tab === 'lead' && <div className="strategy-section-stack"><div className="strategy-card-title"><h3>主动留资</h3><Switch checked={draft.lead_capture.enabled} onChange={enabled => patch(config => {config.lead_capture.enabled=enabled;})} /></div>
          <div className="strategy-check-grid">{(['LINE','微信','电话','Email','WhatsApp'] as const).map(channel => <label key={channel}><input type="checkbox" checked={draft.lead_capture.channels.includes(channel)} onChange={event => patch(config => {config.lead_capture.channels=event.target.checked ? [...config.lead_capture.channels,channel] : config.lead_capture.channels.filter(item => item!==channel);})} />{channel}</label>)}</div>
          <div className="strategy-card-title"><h3>多人咨询交给顾问</h3><Switch checked={draft.handoff.large_group_enabled} onChange={enabled => patch(config => {config.handoff.large_group_enabled=enabled;})} /></div>
          <label className="block-field">达到人数<input type="number" min={2} max={100} value={draft.handoff.large_group_minimum} onChange={event => patch(config => {config.handoff.large_group_minimum=Number(event.target.value);})} /></label>
          <label className="block-field">目录外需求<select value={draft.routing.outside_catalog_action} onChange={event => patch(config => {config.routing.outside_catalog_action=event.target.value as ReceptionConfig['routing']['outside_catalog_action'];})}><option value="recommend_supported_routes">推荐现有线路</option><option value="explain_boundary_only">说明范围</option></select></label>
        </div>}
        {tab === 'silence' && <div className="strategy-section-stack">
          <div className="strategy-card-title"><h3>沉默跟进</h3><Switch checked={draft.silence.enabled} onChange={enabled => patch(config => {config.silence.enabled=enabled;})} /></div>
          <div className="strategy-card-title"><strong>用于真实客户</strong><Switch checked={draft.silence.live_enabled ?? configQuery.data.runtime.live_silence_enabled} disabled={!draft.silence.enabled} onChange={enabled => patch(config => {config.silence.live_enabled=enabled;})} /></div>
          <p>第一项从回复完成计算，后续从上一次跟进计算。</p>
          {(draft.silence.intervals_minutes ?? []).map((minutes,index) => <div className="strategy-field-grid" key={index}><label>第 {index+1} 次间隔（分钟）<input type="number" min={1} max={1434} value={minutes} onChange={event => patch(config => {config.silence.intervals_minutes![index]=Number(event.target.value);})} /></label><button className="icon-button" aria-label={`删除第 ${index+1} 次跟进`} disabled={draft.silence.intervals_minutes!.length===1} onClick={() => patch(config => {config.silence.intervals_minutes!.splice(index,1);})}><Trash2 size={16} /></button></div>)}
          <button className="secondary-button compact" disabled={(draft.silence.intervals_minutes?.length ?? 0)>=20} onClick={() => patch(config => {config.silence.intervals_minutes!.push(120);})}><Plus size={14} />添加跟进</button>
          <div className="strategy-field-grid"><label>开始时间<input type="time" value={draft.silence.active_start} onChange={event => patch(config => {config.silence.active_start=event.target.value;})} /></label><label>结束时间<input type="time" value={draft.silence.active_end} onChange={event => patch(config => {config.silence.active_end=event.target.value;})} /></label><label>每日最多触达<input type="number" min={1} max={20} value={draft.silence.max_proactive_messages_per_day} onChange={event => patch(config => {config.silence.max_proactive_messages_per_day=Number(event.target.value);})} /></label></div>
        </div>}
      </section>
    </div>
    {dirty || uploading.length ? <footer className="strategy-save-bar"><div>{validationError ? <span className="save-error">{validationError}</span> : <span>尚未保存</span>}</div><button className="secondary-button" disabled={!!uploading.length || publish.isPending} onClick={() => {const value=normalizeConfig(configQuery.data.config);setDraft(value);setSaved(JSON.stringify(value));}}>取消修改</button><button className="primary-button" disabled={!!validationError || !!uploading.length || publish.isPending} onClick={() => publish.mutate()}><Save size={15} />{publish.isPending ? '保存中…' : '保存'}</button></footer> : null}
  </div>;
}
