import { useEffect, useState } from 'react';
import { Button, Field, Modal } from './components';
import { host } from './transport';
import { downloadLinks } from './links';
import { explainError } from './errors';
import type { Translate } from './i18n';

type Support = { host: string; url: string; status: 'supported' | 'generic' | 'needs_url' | 'unsupported'; extractors: string[] };
function singleInput(text: string): string {
  try { const links = downloadLinks(text); return links.length === 1 ? links[0] : ''; }
  catch { return /^[a-z\d][a-z\d.-]+\.[a-z]{2,}(?:\/[^\s]*)?$/i.test(text.trim()) ? text.trim() : ''; }
}

export function SiteSupport({ input, version, t }: { input: string; version?: string; t: Translate }) {
  const [open, setOpen] = useState(false); const [manual, setManual] = useState('');
  const query = open ? manual.trim() : singleInput(input);
  const [result, setResult] = useState<{ query: string; support?: Support; error?: string } | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    setResult(null); setBusy(false);
    if (!query) return;
    const controller = new AbortController();
    const timer = setTimeout(() => {
      setBusy(true);
      void host.command<Support>('sites.check', { url: query }, { signal: controller.signal, timeoutMs: 20000 })
        .then(support => { if (!controller.signal.aborted) setResult({ query, support }); })
        .catch(reason => { if (!controller.signal.aborted) setResult({ query, error: String(reason) }); })
        .finally(() => { if (!controller.signal.aborted) setBusy(false); });
    }, 800);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [query, version]);
  const support = result?.query === query ? result.support : undefined;
  const notice = <>
    {busy && <span className="muted" role="status">{t('正在查询网站支持…', 'Checking website support…')}</span>}
    {result?.query === query && result.error && <p className="notice danger" role="alert">{explainError(result.error, t)}</p>}
    {support && <p className={`notice${support.status === 'unsupported' ? ' danger' : ''}`} role="status"><strong>{support.host}</strong> · {support.status === 'supported'
      ? t(`有专用解析支持（${support.extractors.join(', ')}）。具体内容仍可能需要登录或访问权限。`, `A dedicated extractor is available (${support.extractors.join(', ')}). Individual content may require sign-in or access.`)
      : support.status === 'unsupported'
        ? t('当前解析器标记此链接暂不支持下载。请更新 yt-dlp 后重试，或换用其他来源。', 'The current extractor marks this link as unsupported. Update yt-dlp and retry, or use another source.')
        : support.status === 'needs_url'
          ? t('仅凭域名无法确认支持情况，请粘贴具体视频或合集链接。', 'A domain alone cannot confirm support. Paste a specific video or collection URL.')
          : t('未找到此链接的专用解析支持，可能无法下载。仍可尝试通用解析；视频直链和嵌入视频可能可用。', 'No dedicated extractor matched this link; downloading may fail. You can still try generic extraction for direct media or embedded videos.')}</p>}
  </>;
  return <>
    <div className="actions wrap"><Button icon="search" size="small" onClick={() => { setManual(singleInput(input)); setOpen(true); }}>{t('查询网站支持', 'Check website support')}</Button>{!open && notice}</div>
    {open && <Modal title={t('查询网站支持', 'Check website support')} t={t} close={() => setOpen(false)} footer={<Button onClick={() => setOpen(false)}>{t('关闭', 'Close')}</Button>}>
      <Field label={t('网站域名或视频链接', 'Website domain or video URL')}><input autoFocus value={manual} maxLength={8192} placeholder="youtube.com" onChange={event => setManual(event.target.value)} onKeyDown={event => { if (event.key === 'Enter') event.preventDefault(); }} /></Field>
      <p className="hint">{t('使用当前 yt-dlp 和内置插件进行本地匹配，不访问目标网站、不下载内容。未匹配到专用支持不等于整个网站一定无法下载。', 'Matches locally using the current yt-dlp and bundled plugins, without contacting the site or downloading content. No dedicated match does not prove the whole site is unsupported.')}</p>
      {notice}
    </Modal>}
  </>;
}
