import { lazy, Suspense, useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Bell, BookOpenText, Bot, ChevronDown, CircleHelp, FlaskConical, Headphones, LayoutDashboard, LogOut, MapPinned, Menu, MessageCircleMore, Power, Search, Settings as SettingsIcon, Sparkles, Workflow, X } from 'lucide-react';
import { Navigate, Route, Routes, useLocation, useNavigate } from 'react-router-dom';
import { Toast } from './components';
import { useAuth } from './auth';
import Login from './pages/Login';
import { api, ApiError, DEMO_MODE } from './api';

const Conversations = lazy(() => import('./pages/Conversations'));
const Settings = lazy(() => import('./pages/Settings'));
const Overview = lazy(() => import('./pages/Overview'));
const PassiveReply = lazy(() => import('./pages/Automation'));
const Wakeup = lazy(() => import('./pages/Automation').then(module => ({ default: module.Wakeup })));
const EvaluationReports = lazy(() => import('./pages/EvaluationReports'));
const Handoff = lazy(() => import('./pages/Handoff'));
const Sops = lazy(() => import('./pages/Sops'));
const RouteProducts = lazy(() => import('./pages/RouteProducts'));
const AiReceptionStrategy = lazy(() => import('./pages/AiReceptionStrategy'));
const WebKnowledge = lazy(() => import('./pages/WebKnowledge'));
const AiPlayground = lazy(() => import('./pages/AutomationPlayground'));

const navigation = [
  { id: 'overview', label: '运营总览', icon: LayoutDashboard },
  { id: 'playground', label: 'AI 演练场', icon: FlaskConical },
  { id: 'conversations', label: '会话控制台', icon: MessageCircleMore },
  { id: 'handoff', label: '人工接管', icon: Headphones },
  { id: 'evaluation', label: '回放与资料', icon: FlaskConical },
  { id: 'products', label: '线路管理', icon: MapPinned },
  { id: 'ai-strategy', label: 'AI 接待策略', icon: Bot },
  { id: 'knowledge', label: '官网资料库', icon: BookOpenText },
  { id: 'settings', label: '系统设置', icon: SettingsIcon },
];

export default function App() {
  const { user, loading, logout } = useAuth();
  if (loading) return <div className="page-loading">正在验证登录状态...</div>;
  if (!user) return <Login />;
  if (user.must_change_password) return <RequiredPasswordChange />;
  return <AuthenticatedApp onLogout={logout} />;
}

function RequiredPasswordChange() {
  const { user, changePassword, logout } = useAuth();
  const [currentPassword, setCurrentPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const mutation = useMutation({ mutationFn: () => changePassword(currentPassword, newPassword) });
  const localError = newPassword !== confirmPassword && confirmPassword ? '两次输入的新密码不一致' : '';
  return <main className="password-change-page"><section className="password-change-card"><div className="brand-symbol"><Sparkles size={21} /></div><div><span className="eyebrow">ACCOUNT SECURITY</span><h1>首次登录需要修改密码</h1><p>{user?.email} 的临时密码只能用于初始化。本次修改完成后才能进入平台。</p></div><div className="form-stack"><label>当前临时密码<input type="password" value={currentPassword} onChange={(event) => setCurrentPassword(event.target.value)} autoComplete="current-password" /></label><label>新密码<input type="password" value={newPassword} onChange={(event) => setNewPassword(event.target.value)} minLength={10} autoComplete="new-password" /></label><label>确认新密码<input type="password" value={confirmPassword} onChange={(event) => setConfirmPassword(event.target.value)} minLength={10} autoComplete="new-password" /></label></div>{localError || mutation.error ? <div className="login-error">{localError || (mutation.error as ApiError).message}</div> : null}<div className="setting-actions"><button className="primary-button" disabled={!currentPassword || newPassword.length < 10 || newPassword !== confirmPassword || mutation.isPending} onClick={() => mutation.mutate()}>{mutation.isPending ? '正在更新...' : '修改密码并进入'}</button><button className="secondary-button" onClick={() => logout()}>退出登录</button></div></section></main>;
}

function AuthenticatedApp({ onLogout }: { onLogout: () => Promise<void> }) {
  const location = useLocation();
  const navigate = useNavigate();
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [toast, setToast] = useState('');
  const [profileOpen, setProfileOpen] = useState(false);
  const { user } = useAuth();
  const role = user?.roles[0] ?? 'agent';
  const visibleNavigation = navigation.filter((item) => role === 'admin' || (role === 'supervisor' ? !['settings', 'evaluation'].includes(item.id) : ['conversations', 'handoff'].includes(item.id)));
  const queryClient = useQueryClient();
  const notifications = useQuery({ queryKey: ['notifications'], queryFn: () => api<{ items: { id: number; title: string; body: string; conversation_id?: number; read_at?: string }[]; unread: number }>('/notifications'), refetchInterval: 10000 });
  const globalSending = useQuery({
    queryKey: ['global-message-sending'],
    queryFn: () => api<{ enabled: boolean; effective_enabled: boolean }>('/settings/global-message-sending'),
    enabled: !DEMO_MODE,
    refetchInterval: 5000,
  });
  const toggleGlobalSending = useMutation({
    mutationFn: (enabled: boolean) => api<{ enabled: boolean; effective_enabled: boolean }>('/settings/global-message-sending', {
      method: 'PATCH',
      body: JSON.stringify({ enabled }),
    }),
    onSuccess: (data) => {
      queryClient.setQueryData(['global-message-sending'], data);
      setToast(data.enabled ? '全局消息发送已开启' : '全局消息发送已关闭，所有自动消息均已停止');
    },
  });
  const markRead = useMutation({ mutationFn: (id: number) => api(`/notifications/${id}/read`, { method: 'POST' }), onSuccess: () => queryClient.invalidateQueries({ queryKey: ['notifications'] }) });
  const [notificationOpen, setNotificationOpen] = useState(false);
  const page = location.pathname.split('/')[1] || 'conversations';

  useEffect(() => { if (!toast) return; const timer = window.setTimeout(() => setToast(''), 3600); return () => window.clearTimeout(timer); }, [toast]);
  const go = (id: string) => { navigate(`/${id}`); setSidebarOpen(false); };
  const changeGlobalSending = () => {
    const next = !globalSending.data?.enabled;
    if (next && !window.confirm('确定开启全局消息发送吗？开启后，符合接待条件的会话可以向客户发送消息。')) return;
    toggleGlobalSending.mutate(next);
  };

  return <div className="app-shell">
    <aside className={`sidebar ${sidebarOpen ? 'is-open' : ''}`}>
      <div className="brand-block"><span className="brand-symbol"><Sparkles size={21} /></span><div><strong>China2Go</strong><span>AI Operations</span></div><button className="mobile-close" onClick={() => setSidebarOpen(false)} aria-label="关闭导航"><X size={19} /></button></div>
      <div className="workspace-pill"><span className="workspace-avatar">C2</span><div><strong>china2go</strong><span><i />{DEMO_MODE ? '公开演示' : '本地开发'}</span></div><ChevronDown size={15} /></div>
      <nav className="main-nav" aria-label="主要导航">
        <span className="nav-section-label">运营</span>
        {visibleNavigation.filter((item) => !['products', 'ai-strategy', 'knowledge', 'settings'].includes(item.id)).map((item) => { const Icon = item.icon; return <button key={item.id} className={page === item.id ? 'active' : ''} onClick={() => go(item.id)}><Icon size={18} /><span>{item.label}</span></button>; })}
        {visibleNavigation.some((item) => ['products', 'ai-strategy', 'knowledge', 'settings'].includes(item.id)) ? <><span className="nav-section-label management-label">管理</span>{visibleNavigation.filter((item) => ['products', 'ai-strategy', 'knowledge', 'settings'].includes(item.id)).map((item) => { const Icon = item.icon; return <button key={item.id} className={page === item.id ? 'active' : ''} onClick={() => go(item.id)}><Icon size={18} /><span>{item.label}</span></button>; })}</> : null}
      </nav>
      <div className="sidebar-spacer" />
      <div className="sidebar-status"><Bot size={17} /><div><strong>{DEMO_MODE ? '脱敏演示数据' : '自动回复闭环'}</strong><span>{DEMO_MODE ? '操作不会写入 Chatwoot' : '第一阶段开发'}</span></div><i /></div>
    </aside>
    {sidebarOpen ? <button className="sidebar-scrim" onClick={() => setSidebarOpen(false)} aria-label="关闭导航遮罩" /> : null}
    <div className="workspace-main">
      <header className="topbar">
        <div className="topbar-left"><button className="mobile-menu" onClick={() => setSidebarOpen(true)} aria-label="打开导航"><Menu size={20} /></button><div><strong>Operations Command</strong><span>{DEMO_MODE ? '公开脱敏原型 · 不连接真实数据' : 'Chatwoot AI 接管控制'}</span></div>{DEMO_MODE ? <span className="demo-environment-badge">DEMO</span> : null}</div>
        <button className="global-search" onClick={() => go('conversations')}><Search size={16} /><span>{DEMO_MODE ? '搜索演示会话...' : '搜索真实会话...'}</span></button>
        <div className="topbar-actions">{role === 'admin' && !DEMO_MODE ? <button className={`global-send-switch ${globalSending.data?.effective_enabled ? 'is-on' : 'is-off'}`} onClick={changeGlobalSending} disabled={globalSending.isLoading || toggleGlobalSending.isPending} aria-label={globalSending.data?.enabled ? '关闭全局消息发送' : '开启全局消息发送'}><Power size={15} /><span>{globalSending.data?.effective_enabled ? '消息发送已开启' : '全局停发'}</span></button> : null}<div className="profile-wrap"><button className="icon-button" aria-label="通知" onClick={() => setNotificationOpen(!notificationOpen)}><Bell size={18} />{notifications.data?.unread ? <span className="notification-dot" /> : null}</button>{notificationOpen ? <div className="notification-menu"><header><strong>通知</strong><span>{notifications.data?.unread ?? 0} 条未读</span></header>{!notifications.data?.items.length ? <p>暂无通知</p> : notifications.data.items.slice(0, 8).map((item) => <button key={item.id} className={item.read_at ? '' : 'unread'} onClick={() => { markRead.mutate(item.id); if (item.conversation_id) navigate(`/conversations?conversation=${item.conversation_id}`); setNotificationOpen(false); }}><strong>{item.title}</strong><span>{item.body}</span></button>)}</div> : null}</div><button className="icon-button" aria-label="帮助" onClick={() => setToast('开发文档位于 docs/development')}><CircleHelp size={18} /></button><div className="profile-wrap"><button className="profile-button" onClick={() => setProfileOpen(!profileOpen)}><span>{user?.display_name.slice(0, 1)}</span><div><strong>{user?.display_name}</strong><em>{user?.roles[0]}</em></div><ChevronDown size={14} /></button>{profileOpen ? <div className="profile-menu"><button onClick={() => onLogout()}><LogOut size={15} />退出登录</button></div> : null}</div></div>
      </header>
      <main><Suspense fallback={<div className="page-loading">正在加载页面...</div>}><Routes>
        <Route path="/conversations" element={<Conversations notify={setToast} />} />
        <Route path="/settings" element={role === 'admin' ? <Settings notify={setToast} /> : <Navigate to="/conversations" replace />} />
        <Route path="/overview" element={role !== 'agent' ? <Overview onNavigate={go} /> : <Navigate to="/conversations" replace />} />
        <Route path="/playground" element={role !== 'agent' ? <AiPlayground /> : <Navigate to="/conversations" replace />} />
        <Route path="/reply-policy" element={role !== 'agent' ? <PassiveReply /> : <Navigate to="/conversations" replace />} />
        <Route path="/wakeup" element={role !== 'agent' ? <Wakeup /> : <Navigate to="/conversations" replace />} />
        <Route path="/evaluation" element={<EvaluationReports />} />
        <Route path="/handoff" element={<Handoff notify={setToast} />} />
        <Route path="/sops" element={role !== 'agent' ? <Sops notify={setToast} /> : <Navigate to="/conversations" replace />} />
        <Route path="/products" element={role !== 'agent' ? <RouteProducts /> : <Navigate to="/conversations" replace />} />
        <Route path="/ai-strategy" element={role !== 'agent' ? <AiReceptionStrategy /> : <Navigate to="/conversations" replace />} />
        <Route path="/knowledge" element={role !== 'agent' ? <WebKnowledge /> : <Navigate to="/conversations" replace />} />
        <Route path="*" element={<Navigate to={role === 'agent' ? '/conversations' : '/overview'} replace />} />
      </Routes></Suspense></main>
    </div>
    {toast ? <Toast message={toast} onClose={() => setToast('')} /> : null}
  </div>;
}
