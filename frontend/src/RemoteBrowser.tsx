import { useEffect, useRef, useState } from 'react';
import { Button, Empty, Field, Modal } from './components';
import { host } from './transport';
import { explainError } from './errors';
import type { Translate } from './i18n';
import type { Run } from './App';

export function RemoteBrowser({ close, saved, t, run }: { close: () => void; saved: () => void; t: Translate; run: Run }) {
  const [url, setUrl] = useState('https://www.youtube.com/'); const [active, setActive] = useState(false); const [busy, setBusy] = useState(false); const [image, setImage] = useState(''); const [size, setSize] = useState({ width: 1280, height: 720 });
  const [name, setName] = useState('Download login'); const [text, setText] = useState(''); const [status, setStatus] = useState(''); const [error, setError] = useState(''); const frame = useRef<HTMLDivElement>(null);
  useEffect(() => {
    let disposed = false;
    void host.browserGet().then(value => { if (!disposed) { setActive(value.active); if (value.targetUrl) setUrl(value.targetUrl); } }).catch(reason => { if (!disposed) setError(String(reason)); });
    const events = new EventSource('/api/browser/events');
    events.onmessage = message => {
      try { const event = JSON.parse(message.data); if (event.kind === 'browser.frame') { setImage(`data:${event.data.mimeType || 'image/jpeg'};base64,${event.data.data}`); setSize({ width: event.data.width, height: event.data.height }); } else if (event.kind === 'browser.status') { setStatus(String(event.data.message || event.data.status || '')); if (typeof event.data.active === 'boolean') setActive(event.data.active); } else if (event.kind === 'browser.error') setError(String(event.data.error || event.data.message)); } catch { setError(t('浏览器画面数据无效', 'Invalid browser frame data')); }
    };
    return () => { disposed = true; events.close(); };
  }, [t]);
  const stop = () => { setBusy(true); run(async () => { try { await host.browserStop(); close(); } finally { setBusy(false); } }); };
  return <Modal title={t('远程浏览器登录', 'Remote browser sign-in')} subtitle={t('完成来源网站登录后，将 Cookie 保存为下载身份。', 'Sign in to the source site, then save cookies as a download identity.')} wide close={stop} t={t} footer={<><Field label={t('Cookie 名称', 'Cookie name')}><input value={name} maxLength={128} onChange={event => setName(event.target.value)} /></Field><span className="footer-spacer" /><Button disabled={busy} onClick={stop}>{t('关闭浏览器', 'Close browser')}</Button><Button disabled={!active || busy || !name.trim()} tone="primary" icon="check" onClick={() => { setBusy(true); run(async () => { try { await host.browserSave(name); saved(); setStatus(t('Cookie 已保存', 'Cookies saved')); } finally { setBusy(false); } }); }}>{t('保存 Cookie', 'Save cookies')}</Button></>}>
    <form className="toolbar" onSubmit={event => { event.preventDefault(); setBusy(true); run(async () => { try { const parsed = new URL(url); if (!['https:', 'http:'].includes(parsed.protocol) || parsed.username || parsed.password) throw new Error(t('请输入公开 HTTP / HTTPS 链接', 'Enter a public HTTP / HTTPS URL')); await host.browserStart(url); setActive(true); } finally { setBusy(false); } }); }}><input type="url" aria-label={t('登录网站地址', 'Sign-in website URL')} value={url} onChange={event => setUrl(event.target.value)} required /><Button type="submit" icon="external" tone="primary" disabled={busy}>{t('打开', 'Open')}</Button></form>
    <div ref={frame} className="remote-frame" tabIndex={0} role="application" aria-label={t('远程浏览器画面，点击并用键盘操作', 'Remote browser view, click and use the keyboard')}
      onClick={event => { if (!active || !image) return; event.currentTarget.focus(); const bounds = event.currentTarget.getBoundingClientRect(); const scale = Math.min(bounds.width / size.width, bounds.height / size.height); const x = Math.round((event.clientX - bounds.left - (bounds.width - size.width * scale) / 2) / scale); const y = Math.round((event.clientY - bounds.top - (bounds.height - size.height * scale) / 2) / scale); if (x >= 0 && x < size.width && y >= 0 && y < size.height) run(() => host.browserInput({ type: 'click', x, y })); }}
      onKeyDown={event => { if (!active || event.target !== event.currentTarget) return; if (event.key.length === 1 && !event.ctrlKey && !event.metaKey) { event.preventDefault(); run(() => host.browserInput({ type: 'text', text: event.key })); } else if (['Enter', 'Backspace', 'Delete', 'Tab', 'Escape', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) { event.preventDefault(); run(() => host.browserInput({ type: 'key', key: event.key })); } }}
      onWheel={event => { if (active) run(() => host.browserInput({ type: 'scroll', deltaY: Math.round(event.deltaY) })); }}>
      {image ? <img src={image} alt={t('远程登录浏览器', 'Remote sign-in browser')} draggable={false} /> : <Empty title={t('打开来源网站开始登录', 'Open a source site to start signing in')} icon="external" />}
    </div><form className="toolbar remote-text" onSubmit={event => { event.preventDefault(); run(async () => { await host.browserInput({ type: 'text', text }); setText(''); frame.current?.focus(); }); }}><input value={text} autoComplete="off" type="password" aria-label={t('输入并发送到远程浏览器', 'Type and send to the remote browser')} placeholder={t('输入文字，可用于粘贴账号或密码', 'Enter text to paste an account or password')} onChange={event => setText(event.target.value)} /><Button type="submit" disabled={!active || !text}>{t('发送文字', 'Send text')}</Button></form>{error && <p className="notice danger" role="alert">{explainError(error, t)}</p>}{status && <p className="notice" role="status">{status}</p>}
  </Modal>;
}
