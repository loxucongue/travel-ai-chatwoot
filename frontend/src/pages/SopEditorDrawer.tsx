import { useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { ArrowDown, ArrowUp, CalendarDays, Check, Clock3, Image, Info, LoaderCircle, MessageSquareText, Plus, ShieldCheck, Trash2, Upload, Video, X } from 'lucide-react';
import { api, API_BASE } from '../api';
import { Badge } from '../components';
import { contentLabel, emptyNode, newContent, timingLabel } from './sop-types';
import type { ContentType, NodeItem, Sop, SopContent, SopForm } from './sop-types';
import './sop-editor.css';
import MaterialPicker from './MaterialPicker';

const warningLabels: Record<string, string> = {
  previous_message_required: '缺少上一组消息', before_customer_added: '早于客户添加时间',
  outside_initial_channel_window: '超出首次消息的自动回复窗口', outside_contact_hours: '不在 09:00–21:00 联系时段',
  not_after_previous_group: '时间不晚于上一组', contact_frequency_limit: '间隔小于联系人频控', schedule_invalid: '时间规则未完成',
};
type Preview = { items: { key: string; order: number; scheduled_at: string | null; message_count: number; warnings: string[] }[] };
type SopOptions = { labels: { title: string; color: string }[]; locked_labels: string[] };
type Props = { open: boolean; editing: Sop | null; form: SopForm; setForm: (value: SopForm | ((current: SopForm) => SopForm)) => void; updateNode: (index: number, patch: Partial<NodeItem>) => void; save: () => Promise<void>; saving: boolean; notify: (message: string) => void; onClose: () => void };

export default function SopEditorDrawer(props: Props) {
  return props.open ? <Editor {...props} /> : null;
}

function Editor({ editing, form, setForm, updateNode, save, saving, onClose }: Props) {
  const dialog = useRef<HTMLElement>(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  const [uploading, setUploading] = useState<Record<string, boolean>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [saveError, setSaveError] = useState('');
  const [pickerKey, setPickerKey] = useState('');
  const [idsDraft, setIdsDraft] = useState<string | null>(null);
  const idsText = idsDraft ?? form.test_conversation_ids.join(',');
  const idsValid = !idsText.trim() || /^\s*[1-9]\d*(?:\s*[,，]\s*[1-9]\d*)*\s*$/.test(idsText);
  const [previewAt, setPreviewAt] = useState(() => new Date(Date.now() + 8 * 3600000).toISOString().slice(0, 10) + 'T10:00');
  const [previewBody, setPreviewBody] = useState('');
  const inboxes = useQuery({ queryKey: ['inboxes'], queryFn: () => api<{ id: number; chatwoot_inbox_id: number; name: string }[]>('/settings/inboxes') });
  const options = useQuery({ queryKey: ['sop-options'], queryFn: () => api<SopOptions>('/sops/options') });
  const anchorText = form.time_anchor === 'enrollment' ? '本轮入组' : '客户首次进入';
  const activeKeys = new Set(form.nodes.flatMap(node => node.messages.map(item => `${node.key}:${item.key}`)));
  const busy = saving || Object.values(uploading).some(Boolean);
  const invalid = !idsValid || !form.name.trim() || !form.nodes.length || form.frequency_hours < 24 || form.frequency_hours > 720 || form.nodes.some((node, index) =>
    !node.messages.length || (index === 0 && node.basis === 'previous_node' && node.schedule_type === 'relative') ||
    (node.schedule_type === 'calendar_day' && (!node.day_number || node.day_number < 1 || node.day_number > 365 || !node.time_of_day)) ||
    (node.schedule_type === 'fixed' && !node.fixed_at) ||
    (node.schedule_type === 'relative' && (!Number.isFinite(node.delay_minutes) || (node.delay_minutes ?? -1) < 0 || (node.delay_minutes ?? 0) > 525600)) ||
    node.messages.some(m => m.content_type === 'text' ? !m.content.trim() : !m.media_id));
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    dialog.current?.focus();
    const keydown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { event.preventDefault(); closeRef.current(); }
      if (event.key !== 'Tab') return;
      const focusable = Array.from(dialog.current?.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),textarea,select,a[href],[tabindex="0"]') ?? []).filter(x => x.getClientRects().length);
      const first = focusable[0], last = focusable[focusable.length - 1];
      if (event.shiftKey && (document.activeElement === first || document.activeElement === dialog.current)) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    };
    document.addEventListener('keydown', keydown);
    return () => { document.body.style.overflow = overflow; document.removeEventListener('keydown', keydown); previous?.focus(); };
  }, []);
  useEffect(() => {
    const timer = window.setTimeout(() => {
      const date = new Date(`${previewAt}+08:00`);
      setPreviewBody(Number.isNaN(date.getTime()) ? '' : JSON.stringify({ nodes: form.nodes, customer_added_at: date.toISOString(), frequency_hours: form.frequency_hours }));
    }, 350);
    return () => window.clearTimeout(timer);
  }, [form.nodes, form.frequency_hours, previewAt]);
  const preview = useQuery({ queryKey: ['sop-schedule-preview', previewBody], queryFn: () => api<Preview>('/sops/schedule-preview', { method: 'POST', body: previewBody }), enabled: Boolean(previewBody), retry: false });

  function patchContent(nodeKey: string, contentKey: string, patch: Partial<SopContent>) {
    setForm(current => ({ ...current, nodes: current.nodes.map(n => n.key === nodeKey ? { ...n, messages: n.messages.map(m => m.key === contentKey ? { ...m, ...patch } : m) } : n) }));
  }
  function moveNode(index: number, delta: number) {
    setForm(current => { const nodes = [...current.nodes]; [nodes[index], nodes[index + delta]] = [nodes[index + delta], nodes[index]]; return { ...current, nodes }; });
  }
  function moveContent(index: number, item: number, delta: number) {
    const messages = [...form.nodes[index].messages];
    [messages[item], messages[item + delta]] = [messages[item + delta], messages[item]];
    updateNode(index, { messages });
  }
  async function upload(node: NodeItem, item: SopContent, file?: File) {
    if (!file) return;
    if (file.size > 20 * 1024 * 1024 || !file.size) { setErrors(e => ({ ...e, [`${node.key}:${item.key}`]: '文件应大于 0 且不超过 20 MB' })); return; }
    if (!file.type.startsWith(`${item.content_type}/`)) { setErrors(e => ({ ...e, [`${node.key}:${item.key}`]: `请选择${contentLabel(item.content_type)}文件` })); return; }
    setUploading(u => ({ ...u, [`${node.key}:${item.key}`]: true })); setErrors(e => ({ ...e, [`${node.key}:${item.key}`]: '' }));
    try {
      const body = new FormData(); body.append('file', file);
      const result = await api<{ id: number; name: string }>('/media', { method: 'POST', body });
      patchContent(node.key, item.key, { media_id: result.id, media_name: result.name, asset_key: undefined, media_hash: undefined });
    } catch (error) { setErrors(e => ({ ...e, [`${node.key}:${item.key}`]: (error as Error).message })); }
    finally { setUploading(u => ({ ...u, [`${node.key}:${item.key}`]: false })); }
  }
  async function submit() { setSaveError(''); try { await save(); } catch (error) { setSaveError((error as Error).message); } }
  const counts = form.nodes.reduce((sum, node) => sum + node.messages.length, 0);
  return <div className="sop-drawer-backdrop" onMouseDown={event => event.target === event.currentTarget && !busy && onClose()}>
    <section ref={dialog} tabIndex={-1} className="sop-drawer sop-composer" role="dialog" aria-modal="true" aria-labelledby="sop-editor-title">
      <header className="sop-drawer-header"><div><span>SOP / 定时触达</span><h2 id="sop-editor-title">{editing ? '编辑 SOP' : '新建 SOP'}</h2></div><div className="sop-header-actions"><Badge tone="blue"><ShieldCheck size={13} />仅本地演练</Badge><button className="icon-button" title="关闭" aria-label="关闭 SOP 编辑器" disabled={busy} onClick={onClose}><X size={18} /></button></div></header>
      <div className="sop-composer-layout"><div className="sop-drawer-body">
        <section className="sop-editor-section"><header><span>1</span><div><h3>基本信息</h3></div></header><div className="sop-editor-grid">
          <label className="span-2">SOP 名称<input autoComplete="off" value={form.name} maxLength={200} onChange={e => setForm({ ...form, name: e.target.value })} placeholder="例如：新客户行程跟进" /></label>
          <label className="span-2">适用线路<select aria-label="适用线路" value={form.route_variant} onChange={e => setForm({ ...form, route_variant: e.target.value })}><option value="">未限定线路</option><option value="peach_9d_2027">2027 桃花 9 日</option><option value="peach_11d_2027">2027 桃花 + 珠峰 11 日</option></select></label>
          <label>入组事件<select value={form.trigger_type === 'stage' ? 'label' : form.trigger_type} onChange={e => setForm({ ...form, trigger_type: e.target.value, trigger_labels: e.target.value === 'label' ? form.trigger_labels : '' })}><option value="manual">手工选择客户</option><option value="first_message">首次客户消息</option><option value="label">指定会话标签新增</option></select></label>
          <label>时间起点<select value={form.time_anchor} onChange={e => { const basis = e.target.value as SopForm['time_anchor']; setForm({ ...form, time_anchor: basis, nodes: form.nodes.map(node => node.basis === 'previous_node' || node.schedule_type === 'fixed' ? node : { ...node, basis }) }); }}><option value="enrollment">本轮入组时间</option><option value="customer_added">客户首次进入时间</option></select></label>
          {form.trigger_type === 'label' || form.trigger_type === 'stage' ? <TagPicker title="入组标签 · 任意一个新增" value={form.trigger_labels} labels={options.data?.labels ?? []} disabledLabels={options.data?.locked_labels ?? []} onChange={value => setForm({ ...form, trigger_labels: value })} /> : null}
          {options.error && <p className="sop-field-error span-2">标签读取失败：{options.error.message}</p>}
          <fieldset className="sop-inbox-choices span-2"><legend>适用收件箱</legend>{inboxes.isLoading ? <small>正在读取…</small> : inboxes.error ? <span role="alert">{inboxes.error.message}</span> : !inboxes.data?.length ? <small>暂无已同步收件箱</small> : inboxes.data.map(inbox => <label key={inbox.id}><input type="checkbox" checked={form.inbox_ids.includes(inbox.chatwoot_inbox_id)} onChange={e => setForm(current => ({ ...current, inbox_ids: e.target.checked ? [...current.inbox_ids, inbox.chatwoot_inbox_id] : current.inbox_ids.filter(id => id !== inbox.chatwoot_inbox_id) }))} /><span>{inbox.name}</span></label>)}</fieldset>
          <details className="span-2 sop-advanced"><summary>高级设置与备注</summary><label>测试会话 ID（Chatwoot，逗号分隔）<input aria-label="测试会话 ID" value={idsText} onChange={e => { setIdsDraft(e.target.value); setForm({ ...form, test_conversation_ids: e.target.value.split(/[,，]/).filter(x => x.trim()).map(Number).filter(x => Number.isSafeInteger(x) && x > 0) }); }} /></label><label>跨轮次 / 跨策略触达间隔（小时）<input type="number" min={24} max={720} value={form.frequency_hours} onChange={e => setForm({ ...form, frequency_hours: +e.target.value })} /></label><label>内部备注<textarea rows={2} maxLength={2000} value={form.description} onChange={e => setForm({ ...form, description: e.target.value })} placeholder="可选" /></label></details>
        </div></section>
        <section className="sop-editor-section sop-task-section"><header><span>2</span><div><h3>定时内容组</h3></div><button className="secondary-button compact" disabled={form.nodes.length >= 20 || busy} onClick={() => setForm(current => ({ ...current, nodes: [...current.nodes, { ...emptyNode(), basis: 'previous_node' }] }))}><Plus size={15} />添加节点</button></header>
          <div className="sop-task-list">{form.nodes.map((node, index) => {
            const mode = node.schedule_type === 'calendar_day' ? 'day' : node.schedule_type === 'fixed' ? 'legacy' : node.basis === 'previous_node' ? 'previous' : ['customer_added', 'enrollment'].includes(node.basis) ? 'added' : 'legacy';
            return <article className="sop-task-card" key={node.key} aria-label={`节点 ${index + 1}`}><header><div className="sop-task-index"><span>{String(index + 1).padStart(2, '0')}</span><div><strong>内容组 {index + 1}</strong><small>{timingLabel(node)}</small></div></div><div className="sop-order-actions"><button className="icon-button" title="上移节点" aria-label={`上移节点 ${index + 1}`} disabled={!index || busy} onClick={() => moveNode(index, -1)}><ArrowUp size={14} /></button><button className="icon-button" title="下移节点" aria-label={`下移节点 ${index + 1}`} disabled={index === form.nodes.length - 1 || busy} onClick={() => moveNode(index, 1)}><ArrowDown size={14} /></button><button className="icon-button danger-icon" title="删除节点" aria-label={`删除节点 ${index + 1}`} disabled={form.nodes.length === 1 || busy} onClick={() => setForm(current => ({ ...current, nodes: current.nodes.filter(n => n.key !== node.key) }))}><Trash2 size={14} /></button></div></header>
              <div className="sop-timing"><div className="sop-timing-modes" role="group" aria-label={`节点 ${index + 1} 时间类型`}>{([{ id: 'added', label: '相对分钟', icon: Clock3 }, { id: 'day', label: '第 N 天定时', icon: CalendarDays }, { id: 'previous', label: '上一组发送后', icon: MessageSquareText }] as const).map(({ id, label, icon: Icon }) => <button type="button" aria-pressed={mode === id} key={id} disabled={id === 'previous' && index === 0} onClick={() => updateNode(index, { schedule_type: id === 'day' ? 'calendar_day' : 'relative', basis: id === 'previous' ? 'previous_node' : form.time_anchor, fixed_at: undefined })}><Icon size={14} />{label}</button>)}</div>
                {mode === 'day' ? <div className="sop-timing-fields"><span>{anchorText}后第</span><label><span className="sr-only">第几天</span><input aria-label={`节点 ${index + 1} 第几天`} type="number" min={1} max={365} value={node.day_number ?? 1} onChange={e => updateNode(index, { day_number: +e.target.value })} /></label><span>天</span><label><span className="sr-only">发送时刻</span><input aria-label={`节点 ${index + 1} 发送时刻`} type="time" value={node.time_of_day ?? '10:00'} onChange={e => updateNode(index, { time_of_day: e.target.value })} /></label><small>上海时间 · 起点当天为第 1 天</small></div> : mode === 'legacy' ? <div className="sop-legacy-rule"><Info size={14} /><span>保留原规则：{timingLabel(node)}</span></div> : <div className="sop-timing-fields"><span>{mode === 'previous' ? '上一组最后一条确认发送后' : `${anchorText}后`}</span><input aria-label={`节点 ${index + 1} 延迟分钟`} type="number" min={0} max={525600} value={node.delay_minutes ?? 0} onChange={e => updateNode(index, { delay_minutes: +e.target.value })} /><span>分钟</span></div>}
                {index === 0 && mode === 'previous' && <p className="sop-field-error" role="alert">第一个节点需要选择客户添加时间。</p>}
              </div>
              <div className="sop-content-list">{node.messages.map((item, itemIndex) => <div className="sop-content-item" key={item.key} data-content-type={item.content_type}><header><span className="sop-content-number">{itemIndex + 1}</span><select aria-label={`节点 ${index + 1} 内容 ${itemIndex + 1} 类型`} disabled={!!uploading[`${node.key}:${item.key}`]} value={item.content_type} onChange={e => { patchContent(node.key, item.key, { content_type: e.target.value as ContentType, media_id: undefined, media_name: undefined, asset_key: undefined, media_hash: undefined }); setErrors(err => ({ ...err, [`${node.key}:${item.key}`]: '' })); }}>{(['text', 'image', 'video', ...(['audio', 'file'].includes(item.content_type) ? [item.content_type] : [])] as ContentType[]).map(type => <option key={type} value={type}>{contentLabel(type)}</option>)}</select><div className="sop-order-actions"><button className="icon-button" title="上移内容" aria-label={`上移内容 ${index + 1}-${itemIndex + 1}`} disabled={!itemIndex || busy} onClick={() => moveContent(index, itemIndex, -1)}><ArrowUp size={14} /></button><button className="icon-button" title="下移内容" aria-label={`下移内容 ${index + 1}-${itemIndex + 1}`} disabled={itemIndex === node.messages.length - 1 || busy} onClick={() => moveContent(index, itemIndex, 1)}><ArrowDown size={14} /></button><button className="icon-button" title="删除内容" aria-label={`删除内容 ${index + 1}-${itemIndex + 1}`} disabled={node.messages.length === 1 || busy} onClick={() => updateNode(index, { messages: node.messages.filter(m => m.key !== item.key) })}><X size={14} /></button></div></header>
                {item.content_type === 'text' ? <textarea aria-label={`节点 ${index + 1} 文字 ${itemIndex + 1}`} rows={3} maxLength={10000} value={item.content} onChange={e => patchContent(node.key, item.key, { content: e.target.value })} placeholder="输入发送给客户的文字" /> : <div className="sop-local-media">{item.content_type === 'image' && <button type="button" className="secondary-button compact" onClick={() => setPickerKey(pickerKey === `${node.key}:${item.key}` ? '' : `${node.key}:${item.key}`)}><Image size={15} />素材库</button>}{pickerKey === `${node.key}:${item.key}` && <MaterialPicker inboxId={inboxes.data?.find(x => form.inbox_ids.includes(x.chatwoot_inbox_id))?.id} route={form.route_variant} selectedMediaId={item.media_id} onSelect={asset => { patchContent(node.key, item.key, { media_id: asset.media_id, media_name: asset.name, asset_key: asset.key, media_hash: asset.media_hash }); setErrors(e => ({ ...e, [`${node.key}:${item.key}`]: '' })); setPickerKey(''); }} />}{item.media_id && <div className="sop-media-preview">{item.content_type === 'image' ? <img src={`${API_BASE}/media/${item.media_id}/preview`} alt="SOP 图片预览" onError={() => setErrors(current => current[`${node.key}:${item.key}`] ? current : { ...current, [`${node.key}:${item.key}`]: '文件不可预览，请检查或重新上传' })} /> : item.content_type === 'video' ? <video src={`${API_BASE}/media/${item.media_id}/preview`} controls preload="metadata" onError={() => setErrors(current => current[`${node.key}:${item.key}`] ? current : { ...current, [`${node.key}:${item.key}`]: '视频不可播放，请检查文件编码或重新上传' })} /> : <span>附件 #{item.media_id}</span>}</div>}<div className="sop-upload-control"><label className="secondary-button"><input type="file" disabled={!!uploading[`${node.key}:${item.key}`]} accept={item.content_type === 'image' ? 'image/png,image/jpeg,image/webp,image/gif' : item.content_type === 'video' ? 'video/mp4,video/webm' : `${item.content_type}/*`} aria-label={`节点 ${index + 1} 上传${contentLabel(item.content_type)} ${itemIndex + 1}`} onChange={e => { void upload(node, item, e.target.files?.[0]); e.target.value = ''; }} />{uploading[`${node.key}:${item.key}`] ? <LoaderCircle size={15} className="spinning" /> : <Upload size={15} />}{uploading[`${node.key}:${item.key}`] ? '上传中' : item.media_id ? '替换文件' : `上传${contentLabel(item.content_type)}`}</label><span>{item.media_name || (item.media_id ? `本地附件 #${item.media_id}` : '本地存储 · 最大 20 MB')}</span>{item.media_id && <button className="icon-button" title="移除附件" aria-label={`移除附件 ${index + 1}-${itemIndex + 1}`} disabled={busy} onClick={() => { patchContent(node.key, item.key, { media_id: undefined, media_name: undefined, asset_key: undefined, media_hash: undefined }); setErrors(e => ({ ...e, [`${node.key}:${item.key}`]: '' })); }}><Trash2 size={14} /></button>}</div><input aria-label={`节点 ${index + 1} 附件说明 ${itemIndex + 1}`} placeholder="附件说明（可选）" maxLength={10000} value={item.content} onChange={e => patchContent(node.key, item.key, { content: e.target.value })} /></div>}
                {errors[`${node.key}:${item.key}`] && <p className="sop-field-error" role="alert">{errors[`${node.key}:${item.key}`]}</p>}
              </div>)}</div>
              {node.messages.some(item => item.content_type !== 'text') && <label className="sop-reference-choice"><input type="checkbox" checked={!!node.skip_if_materials_provided} onChange={e => updateNode(index, { skip_if_materials_provided: e.target.checked })} />参考图文组：同图已提供时跳过整组</label>}
              <footer className="sop-content-add"><span>添加内容</span>{([{ type: 'text', icon: MessageSquareText }, { type: 'image', icon: Image }, { type: 'video', icon: Video }] as const).map(({ type, icon: Icon }) => <button className="secondary-button compact" key={type} disabled={node.messages.length >= 10 || busy} onClick={() => updateNode(index, { messages: [...node.messages, newContent(type)] })}><Icon size={14} />{contentLabel(type)}</button>)}<small>{node.messages.length}/10</small></footer>
            </article>;
          })}</div>
        </section>
        <section className="sop-editor-section"><header><span>3</span><div><h3>退出与安全</h3></div></header><div className="sop-editor-grid"><TagPicker title="业务退出标签 · 命中任意一个" value={form.exit_labels} labels={options.data?.labels ?? []} disabledLabels={form.trigger_labels.split(',').filter(Boolean)} onChange={value => setForm({ ...form, exit_labels: value })} /><div className="sop-locked-rules span-2"><span><Check size={14} />客户新回复后结束本轮</span><span><Check size={14} />人工 / 退订 / 渠道限制不可关闭</span><span><Check size={14} />结束后须明确重新入组</span></div><details className="span-2 sop-advanced"><summary>固定安全标签</summary><div className="sop-locked-tags">{options.data?.locked_labels.map(tag => <Badge key={tag}>{tag}</Badge>)}</div></details></div></section>
      </div><aside className="sop-schedule-preview"><header><CalendarDays size={18} /><h3>时间线预览</h3></header><label>模拟{anchorText}时间<input type="datetime-local" aria-label="模拟时间起点" value={previewAt} onChange={e => setPreviewAt(e.target.value)} /></label><small>上海时间 · 回复窗口以最新客户消息为准</small><div className="sop-anchor-note"><Info size={14} /><span>{form.time_anchor === 'enrollment' ? '本轮起点与历史首次咨询时间分开记录。' : '首次进入以本收件箱最早同步到的公开客户消息为准。'}</span></div>
        {preview.isFetching && <div className="sop-preview-loading"><LoaderCircle size={14} />计算中</div>}{preview.error && <p className="sop-field-error" role="alert">时间规则未完成，暂时无法预览</p>}
        <ol className="sop-preview-timeline">{preview.data?.items.map(row => <li key={row.key}><span>{row.order}</span><div><strong>{row.scheduled_at ? new Date(row.scheduled_at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }) : '等待上一组确认'}</strong><small>{row.message_count} 条内容 · 按顺序发送</small>{row.warnings.map(warning => <p key={warning}>{warningLabels[warning] || warning}</p>)}</div></li>)}</ol>
        <div className="sop-preview-boundary"><ShieldCheck size={17} /><div><strong>当前仅本地演练</strong><p>同轮按节点间隔；跨轮次 / 策略至少 {Math.max(24, form.frequency_hours)} 小时。</p><p>重新入组不重置渠道窗口。上一组未确认完成，下一组不执行。</p></div></div>
      </aside></div>
      <footer className="sop-drawer-footer"><div>{form.nodes.length} 个时间节点 · {counts} 条内容</div>{saveError && <span className="sop-field-error" role="alert">{saveError}</span>}<button className="secondary-button" disabled={busy} onClick={onClose}>取消</button><button className="primary-button" disabled={invalid || busy || Object.entries(errors).some(([key, value]) => activeKeys.has(key) && Boolean(value))} onClick={submit}>{saving ? '保存中…' : '保存草稿'}</button></footer>
    </section>
  </div>;
}

function TagPicker({ title, value, labels, disabledLabels, onChange }: { title: string; value: string; labels: SopOptions['labels']; disabledLabels: string[]; onChange: (value: string) => void }) {
  const [search, setSearch] = useState('');
  const selected = value.split(',').filter(Boolean);
  const available = [...labels, ...selected.filter(tag => !labels.some(x => x.title === tag)).map(tag => ({ title: tag, color: '#b33a4b' }))];
  return <fieldset className="sop-tag-picker span-2"><legend>{title}</legend><input aria-label={`搜索${title}`} value={search} onChange={event => setSearch(event.target.value)} placeholder="搜索已同步标签" /><div>{available.filter(tag => tag.title.includes(search)).map(tag => <label key={tag.title}><input type="checkbox" checked={selected.includes(tag.title)} disabled={!selected.includes(tag.title) && disabledLabels.includes(tag.title)} onChange={event => onChange((event.target.checked ? [...selected, tag.title] : selected.filter(item => item !== tag.title)).join(','))} /><span className="sop-tag-dot" style={{ backgroundColor: tag.color }} /><span>{tag.title}{!labels.some(x => x.title === tag.title) ? '（未同步）' : ''}</span></label>)}{!available.length && <small>暂无已同步标签</small>}</div></fieldset>;
}
