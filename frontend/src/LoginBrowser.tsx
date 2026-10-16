import { useEffect, useState } from 'react';
import { Button, Field, Modal } from './components';
import { host } from './transport';
import { explainError } from './errors';
import type { Translate } from './i18n';
import type { Run } from './App';
import type { CookieEntry, LoginBrowserMode, LoginBrowserSaved, LoginBrowserStatus } from './types';
type LoginAction = 'embedded' | 'system' | 'save' | 'close' | 'read' | 'refresh';

export function LoginBrowser({ close, saved, t, run, cookieId = '' }: { close: () => void; saved: (result: LoginBrowserSaved) => void; t: Translate; run: Run; cookieId?: string }) {
  const [url, setUrl] = useState('https://www.youtube.com/');
  const [name, setName] = useState(t('下载登录', 'Download login'));
  const [status, setStatus] = useState<LoginBrowserStatus | null>(null);
  const [pending, setPending] = useState<LoginAction | null>(null);
  const busy = pending !== null;
  const [error, setError] = useState('');
  const [cookies, setCookies] = useState<CookieEntry[]>([]);
  const [selectedCookie, setSelectedCookie] = useState(cookieId);
  const [cookieText, setCookieText] = useState<string | null>(null);
  useEffect(() => {
    let disposed = false;
    void host.command<CookieEntry[]>('cookies.list').then(value => {
      if (!disposed) { setCookies(value); setSelectedCookie(current => value.some(cookie => cookie.id === current) ? current : value[0]?.id || ''); }
    }).catch(reason => { if (!disposed) setError(String(reason)); });
    const refresh = () => host.command<LoginBrowserStatus>('browser.login.status').then(value => {
      if (!disposed) { setStatus(value); if (value.active && value.targetUrl) setUrl(value.targetUrl); }
      if (value.cookie) return host.command<CookieEntry[]>('cookies.list').then(cookies => {
        if (!disposed) { setCookies(cookies); setSelectedCookie(current => cookies.some(cookie => cookie.id === current) ? current : value.cookie!.id); }
      });
    }).catch(reason => { if (!disposed) setError(String(reason)); });
    void refresh();
    const timer = window.setInterval(() => { void refresh(); }, 1500);
    return () => { disposed = true; window.clearInterval(timer); };
  }, []);
  const operation = (action: LoginAction, work: () => Promise<void>) => {
    setPending(action); setError('');
    run(async () => { try { await work(); } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); } finally { setPending(null); } });
  };
  const stop = () => { if (!busy) operation('close', async () => { await host.command('browser.login.close'); close(); }); };
  const open = (mode: LoginBrowserMode) => operation(mode, async () => {
    const parsed = new URL(url);
    if (!['https:', 'http:'].includes(parsed.protocol) || parsed.username || parsed.password) throw new Error(t('请输入 HTTP / HTTPS 网站地址', 'Enter an HTTP / HTTPS website URL'));
    setStatus(await host.command<LoginBrowserStatus>('browser.login.open', { url: parsed.href, mode, name: name.trim() }));
  });
  return <Modal title={t('浏览器登录', 'Browser sign-in')} subtitle={t('登录后自动保存 Cookie，下载时按网站自动使用。', 'Cookies save automatically after sign-in and are matched to each download website.')} close={stop} t={t} footer={<>
    {status?.active && <Button icon="refresh" disabled={busy || !name.trim()} onClick={() => operation('save', async () => { const result = await host.command<LoginBrowserSaved>('browser.login.save', { name: name.trim() }); saved(result); setStatus(await host.command<LoginBrowserStatus>('browser.login.status')); setCookies(await host.command<CookieEntry[]>('cookies.list')); })}>{pending === 'save' ? t('同步中…', 'Syncing…') : t('立即同步', 'Sync now')}</Button>}
    <Button tone="primary" disabled={busy} onClick={stop}>{pending === 'close' ? t('关闭中…', 'Closing…') : t('完成', 'Done')}</Button>
  </>}>
    <div className="form-grid">
      <Field label={t('登录网站地址', 'Sign-in website URL')}><input type="url" value={url} maxLength={2048} disabled={busy || status?.active} onChange={event => setUrl(event.target.value)} /></Field>
      <Field label={t('Cookie 名称', 'Cookie name')}><input value={name} maxLength={128} disabled={busy || status?.active} onChange={event => setName(event.target.value)} /></Field>
    </div>
    <div className="actions">
      <Button icon="external" disabled={busy || !status || status.active || !url.trim()} onClick={() => open('embedded')}>{pending === 'embedded' ? t('打开中…', 'Opening…') : t('打开内置浏览器', 'Open built-in browser')}</Button>
      <Button icon="external" disabled={busy || !status?.systemBrowser || status.active || !url.trim()} onClick={() => open('system')}>{pending === 'system' ? t('打开中…', 'Opening…') : t('本机浏览器登录', 'Local browser sign-in')}</Button>
      {status?.active && <Button disabled={busy} onClick={() => operation('close', async () => { await host.command('browser.login.close'); setStatus(await host.command<LoginBrowserStatus>('browser.login.status')); })}>{t('关闭登录窗口', 'Close sign-in window')}</Button>}
    </div>
    <p className={status?.cookie ? 'notice' : 'hint'} role="status">{status?.cookie ? t(`已自动保存 ${status.cookie.count || 0} 个 Cookie，下载时按网站自动携带。`, `${status.cookie.count || 0} cookies saved automatically and matched to each download website.`) : status?.active ? t('正在自动同步 Cookie。登录完成后可直接关闭浏览器窗口。', 'Cookies sync automatically. You can close the browser after signing in.') : t('选择一种登录方式，登录后自动保存。Google 等网站建议用本机浏览器。', 'Choose a sign-in method; cookies save automatically. Use the local browser for sites such as Google.')}</p>
    {status && !status.systemBrowser && <p className="hint">{t('未找到 Edge、Chrome 或 Chromium。安装后重启应用即可使用本机浏览器登录。', 'Edge, Chrome or Chromium was not found. Install one and restart the app to sign in with it.')}</p>}
    <p className="hint">{t('仅将匹配网站域名的 Cookie 用于新任务；已有任务保持原身份。', 'New tasks use cookies matching the website domain. Existing tasks keep their identity.')}</p>
    <details className="help-details"><summary>{t('更多说明', 'More information')}</summary><p>{t('登录状态保留在应用独立的浏览器配置中。Cookie 以本地明文文件保存；删除保存记录不会退出网站账号，退出账号请在浏览器内操作。', 'Sign-in persists in a separate app browser profile. Saved cookies are local plaintext files. Deleting a saved entry does not sign you out; sign out on the website.')}</p></details>
    <section className="saved-cookies" aria-label={t('已保存的 Cookie', 'Saved cookies')}>
      <div className="section-heading"><h3>{t('已保存的 Cookie', 'Saved cookies')}</h3><Button size="small" icon="refresh" disabled={busy} onClick={() => operation('refresh', async () => {
        setCookieText(null);
        const values = await host.command<CookieEntry[]>('cookies.list');
        setCookies(values); setSelectedCookie(current => values.some(cookie => cookie.id === current) ? current : values[0]?.id || '');
      })}>{pending === 'refresh' ? t('刷新中…', 'Refreshing…') : t('刷新已保存 Cookie', 'Refresh saved cookies')}</Button></div>
      {cookies.length ? <>
        <Field label={t('选择已保存 Cookie', 'Select saved cookies')}><select value={selectedCookie} disabled={busy} onChange={event => { setSelectedCookie(event.target.value); setCookieText(null); }}>{cookies.map(cookie => <option key={cookie.id} value={cookie.id}>{cookie.name}</option>)}</select></Field>
        <div className="actions"><Button size="small" disabled={busy || !selectedCookie} onClick={() => operation('read', async () => { setCookieText(null); setCookieText(await host.command<string>('cookies.read', { id: selectedCookie })); })}>{pending === 'read' ? t('读取中…', 'Reading…') : t('查看 Cookie', 'View cookies')}</Button>{cookieText !== null && <Button size="small" disabled={busy} onClick={() => setCookieText(null)}>{t('隐藏 Cookie', 'Hide cookies')}</Button>}</div>
        {cookieText !== null && <Field label={t('Cookie 内容（只读）', 'Cookie contents (read-only)')}><textarea className="cookie-content" value={cookieText} readOnly rows={8} spellCheck={false} autoComplete="off" /></Field>}
      </> : <p className="hint">{t('尚无已保存的 Cookie。登录后会自动显示在这里。', 'No saved cookies yet. They will appear here automatically after sign-in.')}</p>}
    </section>
    {(error || status?.saveError) && <p className="notice danger" role="alert">{error ? explainError(error, t) : <>{t('Cookie 同步失败：', 'Cookie sync failed: ')}{explainError(status!.saveError!, t)} {t('请保持浏览器打开，点击“立即同步”重试。', 'Keep the browser open and choose Sync now to retry.')}</>}</p>}
  </Modal>;
}
