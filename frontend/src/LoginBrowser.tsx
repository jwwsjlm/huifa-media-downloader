import { useEffect, useRef, useState } from 'react';
import { Button, Field } from './components';
import { host } from './transport';
import { explainError } from './errors';
import type { Translate } from './i18n';
import type { Run } from './App';
import type { LoginBrowserMode, LoginBrowserSaved, LoginBrowserStatus } from './types';
type LoginAction = 'embedded' | 'system' | 'save' | 'close';

export function LoginBrowser({ saved, t, run, proxy }: { saved: (result: LoginBrowserSaved) => void; t: Translate; run: Run; proxy?: string }) {
  const [url, setUrl] = useState('https://www.youtube.com/');
  const [preferredMode, setPreferredMode] = useState<LoginBrowserMode>('embedded');
  const initialized = useRef(false);
  const [status, setStatus] = useState<LoginBrowserStatus | null>(null);
  const [pending, setPending] = useState<LoginAction | null>(null);
  const busy = pending !== null;
  const [error, setError] = useState('');
  useEffect(() => {
    let disposed = false;
    const refresh = () => host.command<LoginBrowserStatus>('browser.login.status').then(value => {
      if (!disposed) {
        setStatus(value);
        if (value.active && value.targetUrl) setUrl(value.targetUrl);
        else if (!initialized.current && value.preferredUrl) setUrl(value.preferredUrl);
        if (value.embeddedAvailable === false) setPreferredMode('system');
        else if (!initialized.current && value.preferredMode) setPreferredMode(value.preferredMode);
        initialized.current = true;
      }
    }).catch(reason => { if (!disposed) setError(String(reason)); });
    void refresh();
    const timer = window.setInterval(() => { void refresh(); }, 1500);
    return () => { disposed = true; window.clearInterval(timer); };
  }, []);
  const operation = (action: LoginAction, work: () => Promise<void>) => {
    setPending(action); setError('');
    run(async () => { try { await work(); } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); } finally { setPending(null); } });
  };
  const open = (mode: LoginBrowserMode) => operation(mode, async () => {
    const parsed = new URL(url);
    if (!['https:', 'http:'].includes(parsed.protocol) || parsed.username || parsed.password) throw new Error(t('请输入 HTTP / HTTPS 网站地址', 'Enter an HTTP / HTTPS website URL'));
    setStatus(await host.command<LoginBrowserStatus>('browser.login.open', { url: parsed.href, mode, ...(proxy !== undefined ? { proxy } : {}) }));
    setPreferredMode(mode);
  });
  const embeddedAvailable = status?.embeddedAvailable !== false;
  return <div className="website-sign-in" role="group" aria-label={t('浏览器登录', 'Browser sign-in')}>
    <Field label={t('登录网站地址', 'Sign-in website URL')}><input type="url" value={url} maxLength={2048} disabled={busy || !status || status.active} onChange={event => setUrl(event.target.value)} /></Field>
    <div className="actions">
      {embeddedAvailable && <Button icon="external" tone={preferredMode === 'embedded' ? 'primary' : ''} disabled={busy || !status || status.active || !url.trim()} onClick={() => open('embedded')}>{pending === 'embedded' ? t('打开中…', 'Opening…') : t('打开内置浏览器', 'Open built-in browser')}</Button>}
      <Button icon="external" tone={preferredMode === 'system' ? 'primary' : ''} disabled={busy || !status?.systemBrowser || status.active || !url.trim()} onClick={() => open('system')}>{pending === 'system' ? t('打开中…', 'Opening…') : t('本机浏览器登录', 'Local browser sign-in')}</Button>
      {status?.active && <><Button disabled={busy} onClick={() => operation('close', async () => { await host.command('browser.login.close'); setStatus(await host.command<LoginBrowserStatus>('browser.login.status')); })}>{pending === 'close' ? t('关闭中…', 'Closing…') : t('关闭登录窗口', 'Close sign-in window')}</Button><Button icon="refresh" disabled={busy} onClick={() => operation('save', async () => { const result = await host.command<LoginBrowserSaved>('browser.login.save', { name: t('下载登录', 'Download login') }); saved(result); setStatus(await host.command<LoginBrowserStatus>('browser.login.status')); })}>{pending === 'save' ? t('同步中…', 'Syncing…') : t('立即同步', 'Sync now')}</Button></>}
    </div>
    <p className={status?.cookie ? 'notice' : 'hint'} role="status">{status?.cookie ? t(`已自动保存 ${status.cookie.count || 0} 个 Cookie，下载时按网站自动携带。`, `${status.cookie.count || 0} cookies saved automatically and matched to each download website.`) : status?.active ? t('正在自动同步 Cookie。登录完成后可直接关闭浏览器窗口。', 'Cookies sync automatically. You can close the browser after signing in.') : embeddedAvailable ? t('选择一种登录方式，登录后自动保存。Google 等网站建议用本机浏览器。', 'Choose a sign-in method; cookies save automatically. Use the local browser for sites such as Google.') : t('使用 Chrome、Edge 或 Chromium 登录，Cookie 会自动保存。', 'Sign in with Chrome, Edge or Chromium; cookies save automatically.')}</p>
    {status && !status.systemBrowser && <p className="hint">{t('未找到 Edge、Chrome 或 Chromium。安装后重启应用即可使用本机浏览器登录。', 'Edge, Chrome or Chromium was not found. Install one and restart the app to sign in with it.')}</p>}
    <p className="hint">{t('仅将匹配网站域名的 Cookie 用于新任务；已有任务保持原身份。', 'New tasks use cookies matching the website domain. Existing tasks keep their identity.')}</p>
    <p className="hint">{t('登录状态保留在应用独立的浏览器配置中，Cookie 以本地明文文件自动保存，无需命名或选择。', 'Sign-in persists in a separate app browser profile. Cookies save automatically as local plaintext files; no naming or selection is needed.')}</p>
    {status?.preferenceError && <p className="notice danger" role="alert">{t('上次网址和登录方式未保存：', 'The last address and browser choice could not be saved: ')}{explainError(status.preferenceError, t)}</p>}
    {(error || status?.saveError) && <p className="notice danger" role="alert">{error ? explainError(error, t) : <>{t('Cookie 同步失败：', 'Cookie sync failed: ')}{explainError(status!.saveError!, t)} {status?.active ? t('可点击“立即同步”重试，也可以关闭浏览器；此前已保存的 Cookie 会保留。', 'Choose Sync now to retry, or close the browser. Previously saved cookies are kept.') : t('此前已保存的 Cookie 会保留。请重新打开登录浏览器以重试同步。', 'Previously saved cookies are kept. Reopen the login browser to retry syncing.')}</>}</p>}
  </div>;
}
