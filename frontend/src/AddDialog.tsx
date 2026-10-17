import { useEffect, useMemo, useRef, useState } from 'react';
import { Button, Check, Empty, Field, Modal } from './components';
import { host } from './transport';
import { VirtualList } from './VirtualList';
import { bytes, duration } from './task';
import { commandOptions } from './options';
import { explainError } from './errors';
import type { AddReport, CollectionEntry, CollectionProbe, DownloadOptions, Format, VideoProbe } from './types';
import type { Translate } from './i18n';
import type { Run } from './App';

interface Planned { url: string; options: DownloadOptions }
export function AddDialog({ urls, options, close, added, run, t, initialEntries, selectItems = true }: { urls: string[]; options: DownloadOptions; close: () => void; added: (report?: AddReport) => void; run: Run; t: Translate; initialEntries?: CollectionEntry[]; selectItems?: boolean }) {
  const [busy, setBusy] = useState(true); const [adding, setAdding] = useState(false); const [error, setError] = useState(''); const [entries, setEntries] = useState<CollectionEntry[]>([]); const [selected, setSelected] = useState<Set<string>>(new Set());
  const [search, setSearch] = useState(''); const [filter, setFilter] = useState('all'); const [sort, setSort] = useState('source'); const [active, setActive] = useState('');
  const [plans, setPlans] = useState<Planned[]>(selectItems ? [] : urls.map(url => ({ url, options: { ...options, collectionMode: 'single' } }))); const [index, setIndex] = useState(selectItems ? -1 : 0); const [probe, setProbe] = useState<VideoProbe | null>(null);
  const [video, setVideo] = useState(''); const [audio, setAudio] = useState<Set<string>>(new Set()); const [contentMode, setContentMode] = useState(options.contentMode === 'audio' ? 'audio' : 'video');
  const requestId = useRef(''); const cancelled = useRef(false); const issues = useRef<NonNullable<AddReport['issues']>>([]);
  const cancel = () => { cancelled.current = true; if (requestId.current) void host.command('probe.cancel', { id: requestId.current }).catch(() => {}); if (!adding) close(); };
  useEffect(() => {
    let disposed = false; const id = crypto.randomUUID(); requestId.current = id;
    void (async () => {
      if (!selectItems) return;
      if (initialEntries) { setEntries(initialEntries); setSelected(new Set(initialEntries.filter(entry => entry.available !== false && !entry.error).map(entry => entry.url))); setBusy(false); return; }
      if (urls.length > 1000) throw new Error(t('一次最多添加 1000 个链接', 'Add at most 1000 URLs at once'));
      for (const url of urls) { const parsed = new URL(url); if (!['https:', 'http:'].includes(parsed.protocol) || parsed.username || parsed.password) throw new Error(t('请输入不含登录凭据的 HTTP / HTTPS 链接', 'Enter HTTP / HTTPS URLs without login credentials')); }
      if (options.collectionMode !== 'single') {
        const all: CollectionEntry[] = [];
        for (const url of urls) {
          if (disposed || cancelled.current) return;
          try { const result = await host.command<CollectionProbe>('probe.collection', { url, options: commandOptions(options), requestId: id });
            if (disposed || cancelled.current) return;
            all.push(...(Array.isArray(result.entries) ? result.entries : [{ url, title: result.title || url }]));
          } catch (reason) { if (disposed || cancelled.current) return; all.push({ url, title: url, available: false, error: String(reason) }); }
        }
        const distinct = Array.from(new Map(all.filter(entry => entry.url).map(entry => [entry.url, entry])).values());
        if (!disposed) { setEntries(distinct); setSelected(new Set(distinct.filter(entry => entry.available !== false && !entry.error).map(entry => entry.url))); setBusy(false); }
      } else if (!disposed) { setEntries(urls.map(url => ({ url, title: url }))); setSelected(new Set(urls)); setBusy(false); }
    })().catch(reason => { if (!disposed) { setError(reason instanceof Error ? reason.message : String(reason)); setBusy(false); } });
    return () => { disposed = true; void host.command('probe.cancel', { id }).catch(() => {}); };
  }, [urls, options, t]);
  useEffect(() => {
    if (index < 0 || index >= plans.length) return;
    let disposed = false; const id = crypto.randomUUID(); requestId.current = id; setBusy(true); setError(''); setProbe(null);
    void host.command<VideoProbe>('probe.video', { url: plans[index].url, options: commandOptions(plans[index].options), requestId: id }).then(result => {
      if (disposed) return;
      setProbe(result);
      setVideo(String(result.formats.find(format => format.vcodec && format.vcodec !== 'none')?.formatId || ''));
      setAudio(new Set()); setBusy(false);
    }).catch(reason => { if (!disposed && !cancelled.current) { issues.current.push({ url: plans[index].url, error: String(reason) }); if (index + 1 < plans.length) setIndex(index + 1); else commit(plans.filter(plan => !issues.current.some(issue => issue.url === plan.url))); } });
    return () => { disposed = true; void host.command('probe.cancel', { id }).catch(() => {}); };
  }, [index, plans.length]);
  const entryMap = useMemo(() => Object.fromEntries(entries.map(entry => [entry.url, entry])), [entries]);
  const ids = useMemo(() => entries.filter(entry => (!search || `${entry.title} ${entry.url}`.toLowerCase().includes(search.toLowerCase())) && (filter !== 'selected' || selected.has(entry.url))).map(entry => entry.url).sort((a, b) => sort === 'title' ? entryMap[a].title.localeCompare(entryMap[b].title) : sort === 'duration' ? (entryMap[b].duration || 0) - (entryMap[a].duration || 0) : 0), [entries, entryMap, search, filter, selected, sort]);
  const commit = (values: Planned[]) => {
    setBusy(true); setAdding(true); setError('');
    run(async () => {
      const report: AddReport = { ids: [], results: [], issues: [...entries.filter(entry => entry.error).map(entry => ({ url: entry.url, error: entry.error! })), ...issues.current] };
      try { for (const plan of values) { if (cancelled.current) break; try { const value = await host.command<AddReport>('tasks.add', { urls: [plan.url], options: commandOptions(plan.options), report: true }); if (value?.results) { report.ids.push(...value.ids); report.results.push(...value.results); } } catch (reason) { report.issues!.push({ url: plan.url, error: String(reason) }); } } report.cancelled = cancelled.current; added(report); }
      finally { setBusy(false); setAdding(false); }
    });
  };
  const confirmSelection = () => {
    const chosen = entries.filter(entry => selected.has(entry.url) && entry.available !== false && !entry.error).map(entry => ({ url: entry.url, options: { ...options, collectionMode: 'single' } }));
    if (options.contentMode !== 'manual') { commit(chosen); return; }
    setPlans(chosen); setIndex(0);
  };
  const nextFormat = () => {
    if (!probe) return;
    const next = plans.map((plan, current) => current === index ? { ...plan, options: { ...plan.options, contentMode, formatId: contentMode === 'audio' ? '' : video, videoFormatId: contentMode === 'video' ? video : '', audioFormatIds: [...audio] } } : plan);
    if (index + 1 < next.length) { setPlans(next); setIndex(index + 1); } else commit(next.filter(plan => !issues.current.some(issue => issue.url === plan.url)));
  };
  const toggle = (id: string, checked: boolean) => setSelected(current => { const value = new Set(current); checked ? value.add(id) : value.delete(id); return value; });
  const row = (id: string) => { const entry = entryMap[id]; return <div className="collection-row-content"><Check label={t(`选择 ${entry.title}`, `Select ${entry.title}`)} className="collection-check" checked={selected.has(id)} disabled={entry.available === false || Boolean(entry.error)} onChange={event => toggle(id, event.target.checked)} /><div className="row-main"><strong className="truncate">{entry.title || entry.url}</strong><small className={`truncate ${entry.error ? 'error-text' : ''}`}>{entry.error || entry.url}</small></div><small>{duration(entry.duration)}</small></div>; };
  const formats = probe?.formats || []; const videoFormats = formats.filter(format => format.vcodec && format.vcodec !== 'none'); const audioFormats = formats.filter(format => (!format.vcodec || format.vcodec === 'none') && format.acodec && format.acodec !== 'none');
  return <Modal title={index >= 0 ? t('选择下载格式', 'Choose download format') : t('选择下载项目', 'Choose download items')} subtitle={index >= 0 ? `${index + 1} / ${plans.length} · ${probe?.title || plans[index]?.url}` : t(`共 ${entries.length} 项，已选 ${selected.size} 项`, `${entries.length} items, ${selected.size} selected`)} close={cancel} wide t={t} footer={<><Button onClick={cancel}>{t('取消', 'Cancel')}</Button><Button tone="primary" icon="download" disabled={busy || index < 0 && !selected.size || index >= 0 && (!probe || contentMode === 'video' && !video || contentMode === 'audio' && !audio.size)} onClick={index >= 0 ? nextFormat : confirmSelection}>{busy ? adding ? t('加入队列中…', 'Adding to queue…') : t('解析中…', 'Parsing…') : index >= 0 && index + 1 < plans.length ? t('下一个媒体', 'Next media') : t('下载已选项目', 'Download selected items')}</Button></>}>
    {error && <p className="notice danger" role="alert">{explainError(error, t)}</p>}{busy ? <Empty title={adding ? t('正在加入下载队列…', 'Adding downloads to the queue…') : t('正在解析媒体…', 'Reading media…')} /> : index >= 0 ? <><div className="toolbar"><Button className={contentMode === 'video' ? 'primary' : ''} disabled={!videoFormats.length} onClick={() => setContentMode('video')}>{t('视频', 'Video')}</Button><Button className={contentMode === 'audio' ? 'primary' : ''} disabled={!audioFormats.length} onClick={() => setContentMode('audio')}>{t('音频', 'Audio')}</Button><span className="muted">{t('选择精确来源格式；音轨可多选。', 'Choose exact source formats; select multiple audio tracks.')}</span></div>{contentMode === 'video' && <Field label={t('视频格式', 'Video format')}><select value={video} onChange={event => setVideo(event.target.value)}>{videoFormats.map(format => <option value={formatId(format)} key={formatId(format)}>{formatLabel(format)}</option>)}</select></Field>}<section className="format-audio"><h3>{t('音轨', 'Audio tracks')}</h3>{contentMode === 'video' && <p className="muted">{t('不选择时自动使用最佳匹配音轨。', 'Leave empty to use the best matching audio track.')}</p>}{audioFormats.map(format => <Check key={formatId(format)} label={formatLabel(format)} checked={audio.has(formatId(format))} onChange={event => setAudio(current => { const next = new Set(current); event.target.checked ? next.add(formatId(format)) : next.delete(formatId(format)); return next; })} />)}</section></> : <><div className="toolbar"><input type="search" aria-label={t('搜索合集', 'Search collection')} placeholder={t('搜索标题、链接', 'Search title and URL')} value={search} onChange={event => setSearch(event.target.value)} /><select aria-label={t('筛选项目', 'Filter items')} value={filter} onChange={event => setFilter(event.target.value)}><option value="all">{t('全部项目', 'All items')}</option><option value="selected">{t('已选择', 'Selected')}</option></select><select aria-label={t('合集排序', 'Collection sort')} value={sort} onChange={event => setSort(event.target.value)}><option value="source">{t('来源顺序', 'Source order')}</option><option value="title">{t('标题', 'Title')}</option><option value="duration">{t('时长', 'Duration')}</option></select></div><div className="actions collection-selection"><Button icon="check" onClick={() => setSelected(new Set(entries.filter(entry => entry.available !== false && !entry.error).map(entry => entry.url)))}>{t('全选', 'Select all')}</Button><Button icon="close" onClick={() => setSelected(new Set())}>{t('全不选', 'Select none')}</Button><span className="muted">{t('筛选不会改变已选项目。', 'Filtering keeps selected items.')}</span></div><div className="modal-list-holder">{ids.length ? <VirtualList ids={ids} selected={active} onSelect={setActive} renderRow={row} label={t('合集项目列表', 'Collection item list')} scrollKey="collection" /> : <Empty title={t('没有匹配的项目', 'No matching items')} />}</div></>}
  </Modal>;
}
function formatId(format: Format): string { return String(format.formatId || format.id || format.format_id || ''); }
function formatLabel(format: Format): string { return [formatId(format), format.height ? `${format.height}p` : '', format.ext, format.vcodec !== 'none' ? format.vcodec : format.acodec, format.fps ? `${format.fps} fps` : '', format.language, format.abr ? `${Math.round(format.abr)} kbps` : '', format.filesize ? bytes(format.filesize) : '', format.formatNote || format.format_note].filter(Boolean).join(' · '); }
