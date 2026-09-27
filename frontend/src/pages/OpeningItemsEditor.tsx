import { useState } from 'react';
import { ArrowDown, ArrowUp, LoaderCircle, Plus, Trash2, Upload } from 'lucide-react';
import { API_BASE } from '../api';
import type { OpeningItem, ReceptionConfig } from './reception-config';
import './opening-items-editor.css';

type Props = {
  config: ReceptionConfig;
  patch: (updater: (value: ReceptionConfig) => void) => void;
  uploading: string[];
  errors: Record<string, string>;
  disabled: boolean;
  upload: (item: OpeningItem, file: File) => Promise<void>;
  clearError: (key: string) => void;
  previewChanged: (key: string, status: 'ready' | 'error') => void;
};

function MediaPreview({ item, previewChanged }: { item: OpeningItem; previewChanged: Props['previewChanged'] }) {
  const [failed, setFailed] = useState(false);
  const src = `${API_BASE}/media/${item.media_id}/preview`;
  function report(status: 'ready' | 'error') {
    setFailed(status === 'error');
    previewChanged(`${item.content_type}:${item.media_id}`, status);
  }
  return <div className="opening-media-preview">
    {item.content_type === 'image'
      ? <img src={src} alt={item.media_name || '开场图片'} onLoad={() => report('ready')} onError={() => report('error')} />
      : <video src={src} controls preload="auto" onLoadedData={() => report('ready')} onError={() => report('error')} />}
    {failed && <span className="opening-upload-error" role="alert">{item.content_type === 'video' ? '浏览器无法播放此视频，请检查文件编码或重新上传后再发布。' : '图片无法预览，请检查文件或重新上传后再发布。'}</span>}
  </div>;
}

export default function OpeningItemsEditor({ config, patch, uploading, errors, disabled, upload, clearError, previewChanged }: Props) {
  const items = config.reply.opening_items;
  function update(key: string, changes: Partial<OpeningItem>) {
    patch(value => {
      const item = value.reply.opening_items.find(entry => entry.key === key);
      if (item) Object.assign(item, changes);
    });
  }
  function move(key: string, direction: number) {
    patch(value => {
      const entries = value.reply.opening_items;
      const index = entries.findIndex(item => item.key === key);
      const next = index + direction;
      if (index >= 0 && next >= 0 && next < entries.length) {
        [entries[index], entries[next]] = [entries[next], entries[index]];
      }
    });
  }
  return <section className="opening-message-editor opening-items-editor">
    <div className="strategy-card-title"><h3>首次客户消息开场</h3><button type="button" className="secondary-button compact" disabled={disabled || items.length >= 10} onClick={() => {
      const key = `opening-${crypto.randomUUID()}`;
      patch(value => { if (value.reply.opening_items.length < 10) value.reply.opening_items.push({ key, content_type: 'text', content: '' }); });
    }}><Plus size={14} />新增消息</button></div>
    {items.map((item, index) => {
      const busy = uploading.includes(item.key);
      return <div className="opening-message-row" key={item.key} data-opening-key={item.key}>
        <div className="opening-message-actions">
          <strong>消息 {index + 1}</strong>
          <select aria-label={`消息 ${index + 1} 类型`} value={item.content_type} disabled={disabled || busy} onChange={event => {
            update(item.key, { content_type: event.target.value as OpeningItem['content_type'], media_id: null, media_hash: undefined, media_name: undefined });
            clearError(item.key);
          }}><option value="text">文字</option><option value="image">图片</option><option value="video">视频</option></select>
          <span>{Array.from(item.content).length} / {config.reply.max_characters}</span>
          <button type="button" className="icon-button" title="上移消息" aria-label={`上移消息 ${index + 1}`} disabled={disabled || index === 0} onClick={() => move(item.key, -1)}><ArrowUp size={16} /></button>
          <button type="button" className="icon-button" title="下移消息" aria-label={`下移消息 ${index + 1}`} disabled={disabled || index === items.length - 1} onClick={() => move(item.key, 1)}><ArrowDown size={16} /></button>
          <button type="button" className="icon-button" title="删除消息" aria-label={`删除消息 ${index + 1}`} disabled={disabled || busy || items.length === 1} onClick={() => {
            patch(value => { if (value.reply.opening_items.length > 1) value.reply.opening_items = value.reply.opening_items.filter(entry => entry.key !== item.key); });
            clearError(item.key);
          }}><Trash2 size={16} /></button>
        </div>
        {item.content_type === 'text'
          ? <label className="block-field"><textarea aria-label={`开场消息 ${index + 1}`} disabled={disabled} rows={3} value={item.content} onChange={event => update(item.key, { content: event.target.value })} /></label>
          : <div className="opening-media-editor">
            {!!item.media_id && <MediaPreview key={`${item.content_type}:${item.media_id}`} item={item} previewChanged={previewChanged} />}
            <div className="opening-upload-control">
              <label className={`secondary-button${disabled || busy ? ' is-disabled' : ''}`}>
                <input type="file" disabled={disabled || busy} aria-label={`上传${item.content_type === 'image' ? '图片' : '视频'} ${index + 1}`} accept={item.content_type === 'image' ? '.png,.jpg,.jpeg,.webp,.gif,image/png,image/jpeg,image/webp,image/gif' : '.mp4,.webm,video/mp4,video/webm'} onChange={event => {
                  const file = event.target.files?.[0];
                  event.target.value = '';
                  if (file) void upload(item, file);
                }} />
                {busy ? <LoaderCircle size={15} className="spinning" /> : <Upload size={15} />}
                {busy ? '上传中' : item.media_id ? '替换文件' : '上传文件'}
              </label>
              <span>{item.media_name || (item.media_id ? `附件 #${item.media_id}` : item.content_type === 'image' ? 'PNG / JPEG / WebP / GIF · 最大 20 MB' : 'MP4 / WebM · 最大 20 MB')}</span>
              {!!item.media_id && <button type="button" className="icon-button" title="移除附件" aria-label={`移除附件 ${index + 1}`} disabled={disabled || busy} onClick={() => {
                update(item.key, { media_id: null, media_hash: undefined, media_name: undefined });
                clearError(item.key);
              }}><Trash2 size={16} /></button>}
            </div>
            {errors[item.key] && <div className="opening-upload-error" role="alert">{errors[item.key]}</div>}
            <label className="block-field">附件说明（可选）<textarea aria-label={`附件说明 ${index + 1}`} disabled={disabled} rows={2} value={item.content} onChange={event => update(item.key, { content: event.target.value })} /></label>
          </div>}
      </div>;
    })}
    <label className="compact-number-field">消息间隔<input aria-label="开场消息间隔" type="number" min={1} max={30} disabled={disabled} value={config.reply.opening_interval_seconds} onChange={event => patch(value => { value.reply.opening_interval_seconds = Number(event.target.value); })} />秒</label>
  </section>;
}
