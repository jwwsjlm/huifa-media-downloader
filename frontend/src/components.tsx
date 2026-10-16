import { memo, useEffect, useId, useRef, useState, type ButtonHTMLAttributes, type ReactNode } from 'react';
import type { Translate } from './i18n';

const paths: Record<string, string> = {
  download: 'M12 3v12m-5-5 5 5 5-5M4 16v4h16v-4', library: 'M4 3v18M8 3v18m4-18v18M17 3l4 17',
  settings: 'M4 7h16M4 17h16M8 4v6m8 4v6', info: 'M12 11v6m0-10h.01M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0',
  folder: 'M3 7h6l2 2h10v11H3V7Zm0 0V4h7l2 3', plus: 'M12 5v14M5 12h14', close: 'm6 6 12 12M6 18 18 6',
  play: 'm8 4 12 8-12 8V4Z', pause: 'M8 4v16M16 4v16', check: 'm5 12 4 4L19 6',
  refresh: 'M20 7v5h-5M4 17v-5h5m-4-3a8 8 0 0 1 13-4l2 2M4 17l2 2a8 8 0 0 0 13-4',
  copy: 'M9 9h12v12H9V9ZM5 15H3V3h12v2', image: 'M3 3h18v18H3V3Zm0 14 5-5 4 4 4-4 5 5M9 7h.01',
  trash: 'M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7', external: 'M14 3h7v7m0-7L10 14M10 3H3v18h18v-7',
  video: 'M3 5h12v14H3V5Zm12 5 6-4v12l-6-4', search: 'm21 21-5-5M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0',
};
export function Icon({ name }: { name: string }) { return <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={paths[name] || paths.info} /></svg>; }
export function Button({ icon, tone = '', size = 'default', className = '', children, ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { icon?: string; tone?: string; size?: 'default' | 'small' }) { return <button type="button" className={`button ${size === 'small' ? 'small' : ''} ${tone} ${className}`} {...props}>{icon && <Icon name={icon} />}{children}</button>; }
export function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) { return <label className="field"><span className="field-label">{label}</span>{children}{hint && <small>{hint}</small>}</label>; }
export function Check({ label, ...props }: Omit<React.InputHTMLAttributes<HTMLInputElement>, 'type'> & { label: string }) { return <label className="check"><input type="checkbox" {...props} /><span>{label}</span></label>; }
export function Empty({ title, detail, icon = 'download' }: { title: string; detail?: string; icon?: string }) { return <div className="empty"><Icon name={icon} /><h3>{title}</h3>{detail && <p>{detail}</p>}</div>; }
export function Heading({ title, subtitle, children }: { title: string; subtitle?: string; children?: ReactNode }) { return <header className="page-header"><div><h1>{title}</h1>{subtitle && <p>{subtitle}</p>}</div><div className="actions">{children}</div></header>; }
export function Badge({ children, tone = '' }: { children: ReactNode; tone?: string }) { return <span className={`badge ${tone}`}>{children}</span>; }
export function Modal({ title, subtitle, children, footer, close, wide = false, t }: { title: string; subtitle?: string; children: ReactNode; footer?: ReactNode; close: () => void; wide?: boolean; t: Translate }) {
  const ref = useRef<HTMLDialogElement>(null); const titleId = useId();
  useEffect(() => {
    const dialog = ref.current;
    const focus = document.activeElement as HTMLElement | null;
    dialog?.showModal();
    return () => { dialog?.close(); focus?.focus({ preventScroll: true }); };
  }, []);
  return <dialog ref={ref} className={`dialog ${wide ? 'wide' : ''}`} aria-labelledby={titleId} onCancel={event => { event.preventDefault(); close(); }}>
    <header className="dialog-header"><div><h2 id={titleId}>{title}</h2>{subtitle && <p className="muted">{subtitle}</p>}</div><Button icon="close" tone="icon-only" aria-label={t('关闭对话框', 'Close dialog')} onClick={close} /></header>
    <div className="dialog-body">{children}</div><footer className="dialog-footer">{footer || <Button onClick={close}>{t('关闭', 'Close')}</Button>}</footer>
  </dialog>;
}
export const Thumbnail = memo(function Thumbnail({ url, large = false }: { url?: string; large?: boolean }) {
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [url]);
  return <div className={`thumbnail ${large ? 'large' : ''}`}><Icon name="video" />{url && !failed && <img src={url} alt="" loading="lazy" onError={() => setFailed(true)} />}</div>;
});
