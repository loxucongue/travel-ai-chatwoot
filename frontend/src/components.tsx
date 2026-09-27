import type { ReactNode } from 'react';
import { AlertCircle, Check, ChevronRight, LoaderCircle, LockKeyhole, SearchX, X } from 'lucide-react';

export function Badge({ tone = 'neutral', children }: { tone?: 'neutral' | 'green' | 'amber' | 'red' | 'blue'; children: ReactNode }) {
  return <span className={`badge badge-${tone}`}>{children}</span>;
}

export function Avatar({ initials, tone = 0, size = 'md' }: { initials: string; tone?: number; size?: 'sm' | 'md' | 'lg' }) {
  return <span className={`avatar avatar-${size} avatar-tone-${tone % 5}`}>{initials}</span>;
}

export function Toggle({ checked, onChange, label, disabled = false }: { checked: boolean; onChange: (checked: boolean) => void; label: string; disabled?: boolean }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      className={`toggle ${checked ? 'is-on' : ''}`}
      onClick={() => onChange(!checked)}
      title={label}
    >
      <span />
    </button>
  );
}

export function PageHeader({ title, description, eyebrow, actions }: { title: string; description: string; eyebrow?: string; actions?: ReactNode }) {
  return (
    <header className="page-header">
      <div>
        {eyebrow ? <div className="eyebrow">{eyebrow}</div> : null}
        <h1>{title}</h1>
        {description ? <p>{description}</p> : null}
      </div>
      {actions ? <div className="page-actions">{actions}</div> : null}
    </header>
  );
}

export function MetricCard({ label, value, detail, tone = 'neutral', icon }: { label: string; value: string; detail: string; tone?: 'neutral' | 'green' | 'amber' | 'red' | 'blue'; icon: ReactNode }) {
  return (
    <article className="metric-card">
      <div className={`metric-icon metric-icon-${tone}`}>{icon}</div>
      <div className="metric-label">{label}</div>
      <div className="metric-value">{value}</div>
      <div className={`metric-detail metric-detail-${tone}`}>{detail}</div>
    </article>
  );
}

export function EmptyState({ type = 'empty', title, description, action }: { type?: 'empty' | 'error' | 'loading' | 'permission'; title: string; description: string; action?: ReactNode }) {
  const icon = {
    empty: <SearchX size={24} />,
    error: <AlertCircle size={24} />,
    loading: <LoaderCircle size={24} className="spin" />,
    permission: <LockKeyhole size={24} />,
  }[type];
  return (
    <div className={`empty-state empty-state-${type}`}>
      <div className="empty-icon">{icon}</div>
      <strong>{title}</strong>
      <p>{description}</p>
      {action}
    </div>
  );
}

export function Modal({ open, title, description, children, footer, onClose }: { open: boolean; title: string; description?: string; children: ReactNode; footer: ReactNode; onClose: () => void }) {
  if (!open) return null;
  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <section className="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title">
        <header className="modal-header">
          <div>
            <h2 id="modal-title">{title}</h2>
            {description ? <p>{description}</p> : null}
          </div>
          <button className="icon-button" onClick={onClose} aria-label="关闭"><X size={18} /></button>
        </header>
        <div className="modal-body">{children}</div>
        <footer className="modal-footer">{footer}</footer>
      </section>
    </div>
  );
}

export function Toast({ message, onClose }: { message: string; onClose: () => void }) {
  return (
    <div className="toast" role="status">
      <Check size={16} />
      <span>{message}</span>
      <button onClick={onClose} aria-label="关闭提示"><X size={14} /></button>
    </div>
  );
}

export function DetailRow({ label, children, action }: { label: string; children: ReactNode; action?: ReactNode }) {
  return <div className="detail-row"><span>{label}</span><div>{children}</div>{action}</div>;
}

export function StatusDot({ tone = 'green' }: { tone?: 'green' | 'amber' | 'red' | 'blue' }) {
  return <span className={`status-dot status-dot-${tone}`} />;
}

export function InlineLink({ children, onClick }: { children: ReactNode; onClick?: () => void }) {
  return <button className="inline-link" onClick={onClick}>{children}<ChevronRight size={14} /></button>;
}
