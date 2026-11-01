import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { RefreshCw, Save } from 'lucide-react';
import { api } from '../api';
import { Badge, EmptyState } from '../components';

interface Channel { id: number; name: string; channel_type: string; chatwoot_inbox_id: number; status: string; enabled: boolean; activation_mode: 'automatic' | 'manual'; }
interface ChannelSettings { items: Channel[]; revision: string; global_sending_enabled: boolean; }

export default function ChannelAiSettings({ notify }: { notify: (message: string) => void }) {
  const client = useQueryClient();
  const global = useQuery({ queryKey: ['global-message-sending'], queryFn: () => api<{ enabled: boolean; effective_enabled: boolean }>('/settings/global-message-sending'), refetchInterval: 5000 });
  const globalSending = global.data?.effective_enabled === true;
  const query = useQuery({ queryKey: ['channel-ai'], queryFn: () => api<ChannelSettings>('/settings/channel-ai'), refetchOnWindowFocus: false, refetchOnReconnect: false });
  const [draft, setDraft] = useState<ChannelSettings | null>(null);
  const [saved, setSaved] = useState(false);
  const value = draft ?? query.data;
  const dirty = !!draft;
  const save = useMutation({
    mutationFn: () => api<ChannelSettings>('/settings/channel-ai', { method: 'PATCH', body: JSON.stringify({
      revision: draft!.revision,
      changes: draft!.items.filter(item => { const prior=query.data?.items.find(row => row.id===item.id); return item.enabled!==prior?.enabled || item.activation_mode!==prior?.activation_mode; })
        .map(item => ({ id: item.id, enabled: item.enabled, activation_mode: item.activation_mode })),
    }) }),
    onSuccess: async data => {
      client.setQueryData(['channel-ai'], data);
      setDraft(null); setSaved(true);
      notify('渠道 AI 设置已保存');
      await Promise.all(['inboxes', 'settings-inboxes', 'reception-config', 'channel-ai'].map(key => client.invalidateQueries({ queryKey: [key] })));
    },
  });
  const changed = value?.items.some(item => { const prior=query.data?.items.find(row => row.id===item.id); return item.enabled!==prior?.enabled || item.activation_mode!==prior?.activation_mode; });
  if (query.isLoading) return <EmptyState type="loading" title="正在读取渠道 AI 设置" description="" />;
  if (!value) return <EmptyState type="error" title="渠道设置读取失败" description={(query.error as Error)?.message ?? '请刷新重试'} />;
  const status = (item: Channel) => {
    if (!item.enabled) return 'AI 已关闭';
    if (item.status !== 'active') return '渠道不可用';
    const mode = item.activation_mode === 'manual' ? '人工开启 AI' : '自动接待';
    return globalSending ? mode : `${mode} · 全局发送暂停`;
  };
  return <>
    <header className="settings-section-head"><div><h2>渠道 AI</h2><p>统一设置每个渠道的 AI 权限与开启方式。修改后点击保存。</p></div><Badge tone="blue">全局设置</Badge></header>
    <p>关闭不会影响人工收发。开启后仍遵守全局发送开关、人工接管、停止标签及渠道回复期限。</p>
    <p>人工开启：新客进入时不自动接待。顾问了解需求后，移除“人工接管”并添加“ai”，AI会接续当前会话中尚未回答的问题。此模式不会在人工回复30分钟后自行开启AI；已手动开启的会话继续按原有权限服务。</p>
    {!globalSending && <div className="info-box">全局消息发送当前暂停；保存渠道设置不会自动打开全局发送。</div>}
    {query.error || save.error ? <div className="login-error" role="alert">{((save.error || query.error) as Error).message}</div> : null}
    <div className="responsive-table"><table><thead><tr><th>渠道 / 收件箱</th><th>连接状态</th><th>允许 AI</th><th>开启方式</th><th>已保存状态</th></tr></thead><tbody>
      {value.items.map(item => {
        const persisted = query.data!.items.find(row => row.id === item.id)!;
        return <tr key={item.id}><td><strong>{item.name}</strong><p>{item.channel_type} · {item.chatwoot_inbox_id}</p></td>
          <td><Badge tone={item.status === 'active' ? 'green' : 'amber'}>{item.status === 'active' ? '正常' : '不可用'}</Badge></td>
          <td><label><input type="checkbox" aria-label={`${item.name} 允许 AI`} checked={item.enabled}
            disabled={save.isPending || (item.status !== 'active' && !item.enabled)} onChange={event => {
              const enabled = event.target.checked; setSaved(false); save.reset();
              setDraft({ ...value, items: value.items.map(row => row.id === item.id ? { ...row, enabled } : row) });
            }} />{item.enabled ? '允许' : '关闭'}</label>{item.enabled !== persisted.enabled && <p>待保存：{item.enabled ? '开启 AI' : '关闭 AI'}</p>}</td>
          <td><select aria-label={`${item.name} 开启方式`} value={item.activation_mode} disabled={!item.enabled || save.isPending} onChange={event => {
            const activation_mode=event.target.value as Channel['activation_mode']; setSaved(false); save.reset();
            setDraft({...value,items:value.items.map(row => row.id===item.id ? {...row,activation_mode} : row)});
          }}><option value="automatic">自动接待新客</option><option value="manual">人工添加 ai 后接待</option></select>{item.activation_mode!==persisted.activation_mode && <p>开启方式待保存</p>}</td>
          <td><Badge tone={persisted.enabled && persisted.status === 'active' && globalSending ? 'green' : 'amber'}>{status(persisted)}</Badge></td></tr>;
      })}
    </tbody></table></div>
    {!value.items.length && <EmptyState title="尚未同步渠道" description="请到 Chatwoot 连接中同步资源，新渠道默认不会自动接待。" />}
    <div className="setting-actions"><span role="status">{changed ? '有未保存修改' : saved ? '已保存，状态已更新' : '以上为当前已保存状态'}</span>
      <button className="secondary-button" disabled={save.isPending} onClick={async () => { setDraft(null); setSaved(false); save.reset(); await query.refetch(); }}><RefreshCw size={15} />{dirty ? '放弃修改并重新读取' : '重新读取'}</button>
      <button className="primary-button" disabled={!changed || save.isPending} onClick={() => save.mutate()}><Save size={15} />{save.isPending ? '保存中…' : '保存渠道 AI 设置'}</button>
    </div>
  </>;
}
