import { useEffect, useRef, useState, type FormEvent } from 'react';
import { invoke } from '@tauri-apps/api/core';
import { Button } from './components';
import './BrowserToolbar.css';

type NavigationState = { url: string; canGoBack: boolean; canGoForward: boolean };
type NavigationAction = 'back' | 'forward' | 'reload' | 'navigate';

export function BrowserToolbar() {
  const zh = navigator.language.toLowerCase().startsWith('zh');
  const t = (chinese: string, english: string) => zh ? chinese : english;
  const [state, setState] = useState<NavigationState | null>(null);
  const [address, setAddress] = useState('');
  const [pending, setPending] = useState<NavigationAction | null>(null);
  const [error, setError] = useState('');
  const input = useRef<HTMLInputElement>(null);
  const editing = useRef(false);
  const busy = useRef(false);
  const revision = useRef(0);
  const mounted = useRef(true);
  const applyState = (next: NavigationState) => {
    setState(next);
    if (!editing.current) setAddress(next.url);
  };

  useEffect(() => {
    mounted.current = true;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      const current = revision.current;
      if (!busy.current) {
        try {
          const next = await invoke<NavigationState>('login_navigation', { action: 'state' });
          if (!disposed && current === revision.current) applyState(next);
        } catch (reason) {
          if (!disposed && current === revision.current) setError(reason instanceof Error ? reason.message : String(reason));
        }
      }
      if (!disposed) timer = setTimeout(poll, 750);
    };
    const focusAddress = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'l') {
        event.preventDefault();
        input.current?.focus();
        input.current?.select();
      }
    };
    void poll();
    window.addEventListener('keydown', focusAddress);
    return () => {
      disposed = true;
      mounted.current = false;
      clearTimeout(timer);
      window.removeEventListener('keydown', focusAddress);
    };
  }, []);

  const navigate = async (action: NavigationAction, url?: string) => {
    if (busy.current) return;
    busy.current = true;
    revision.current += 1;
    setPending(action);
    setError('');
    try {
      const next = await invoke<NavigationState>('login_navigation', { action, ...(url === undefined ? {} : { url }) });
      if (mounted.current) applyState(next);
    } catch (reason) {
      if (mounted.current) setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      busy.current = false;
      if (mounted.current) setPending(null);
    }
  };
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!address.trim() || busy.current) return;
    editing.current = false;
    void navigate('navigate', address.trim());
  };

  return <nav className="browser-toolbar" aria-label={t('浏览器导航', 'Browser navigation')} aria-busy={Boolean(pending)}>
    <Button size="small" tone="icon-only" className="browser-history" aria-label={t('返回', 'Back')} title={t('返回', 'Back')} disabled={!state?.canGoBack || Boolean(pending)} onClick={() => void navigate('back')}><span aria-hidden="true">‹</span></Button>
    <Button size="small" tone="icon-only" className="browser-history" aria-label={t('前进', 'Forward')} title={t('前进', 'Forward')} disabled={!state?.canGoForward || Boolean(pending)} onClick={() => void navigate('forward')}><span aria-hidden="true">›</span></Button>
    <Button size="small" tone="icon-only" icon="refresh" className={pending ? 'browser-refresh busy' : 'browser-refresh'} aria-label={pending ? t('加载中…', 'Loading…') : t('刷新', 'Refresh')} title={t('刷新', 'Refresh')} disabled={!state || Boolean(pending)} onClick={() => void navigate('reload')} />
    <form onSubmit={submit}>
      <input ref={input} type="text" aria-label={t('网站地址', 'Website address')} aria-invalid={Boolean(error)} aria-describedby={error ? 'browser-navigation-error' : undefined} value={address} placeholder={t('输入网站地址，按回车访问', 'Enter a website address and press Enter')} spellCheck={false} autoComplete="off" autoCapitalize="off" onFocus={() => { editing.current = true; }} onBlur={() => { editing.current = false; }} onChange={event => { editing.current = true; setAddress(event.target.value); }} onKeyDown={event => {
        if (event.key === 'Escape') {
          setAddress(state?.url || '');
          editing.current = false;
          input.current?.blur();
        }
      }} />
    </form>
    {error && <span id="browser-navigation-error" className="browser-navigation-error" role="alert" title={error}>{error}</span>}
  </nav>;
}
