import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Search, Check } from 'lucide-react';
import { api, API_BASE } from '../api';
import './material-library.css';

type Material = { key: string; name: string; media_id: number; media_hash: string; content_type: string; ready: boolean; notes: string[] };
export default function MaterialPicker({ inboxId, route, selectedMediaId, onSelect }: { inboxId?: number; route: string; selectedMediaId?: number; onSelect: (item: Material) => void }) {
  const [search, setSearch] = useState('');
  const result = useQuery({ queryKey: ['materials', inboxId, route], queryFn: () => api<{ items: Material[] }>(`/materials?inbox_binding_id=${inboxId}&route_variant=${encodeURIComponent(route)}`), enabled: !!inboxId });
  const items = result.data?.items.filter(x => x.ready && x.content_type === 'image' && x.name.includes(search)) ?? [];
  return <section className="material-picker" aria-label="共享图片素材库">
    <label><Search size={15} /><input aria-label="搜索素材" placeholder="搜索素材名称" value={search} onChange={e => setSearch(e.target.value)} /></label>
    {!inboxId ? <p>请先选择一个收件箱</p> : result.isLoading ? <p>正在读取素材…</p> : result.error ? <p role="alert">{result.error.message}</p> : !items.length ? <p>暂无匹配的可用素材</p> : <div className="material-picker-grid">{items.map(item => <button type="button" key={item.key} aria-pressed={item.media_id === selectedMediaId} onClick={() => onSelect(item)} title={item.name}>
      <img src={`${API_BASE}/media/${item.media_id}/preview`} alt={item.name} decoding="async" />
      <span>{item.name}</span>{item.media_id === selectedMediaId && <Check size={14} />}
    </button>)}</div>}
  </section>;
}
