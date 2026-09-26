import { useState } from 'react';
import { Bot, LoaderCircle, LockKeyhole } from 'lucide-react';
import { ApiError } from '../api';
import { useAuth } from '../auth';

export default function Login() {
  const { login } = useAuth();
  const [email, setEmail] = useState('admin@luoxuecong.asia');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setSubmitting(true); setError('');
    try { await login(email, password); }
    catch (value) { setError(value instanceof ApiError ? value.message : '登录失败，请检查后端服务'); }
    finally { setSubmitting(false); }
  };

  return <main className="login-page">
    <section className="login-panel">
      <header><span><Bot size={22} /></span><div><strong>China2Go</strong><small>AI Operations</small></div></header>
      <div className="login-title"><LockKeyhole size={20} /><div><h1>登录运营平台</h1><p>使用本平台账号访问已授权的 Chatwoot 收件箱。</p></div></div>
      <form onSubmit={submit} className="form-stack">
        <label>邮箱<input type="email" value={email} onChange={(event) => setEmail(event.target.value)} autoComplete="username" required /></label>
        <label>密码<input type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" required /></label>
        {error ? <div className="login-error">{error}</div> : null}
        <button className="primary-button login-submit" disabled={submitting}>{submitting ? <><LoaderCircle className="spin" size={16} />登录中</> : '登录'}</button>
      </form>
      <footer>首次使用请在本机运行管理员初始化命令。</footer>
    </section>
  </main>;
}
