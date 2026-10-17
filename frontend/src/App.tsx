import { memo, useCallback, useEffect, useMemo, useRef, useState, type Dispatch } from 'react';
import { host } from './transport';
import { useApp, type Action, type AppState, type ViewState } from './state';
import { localeFor, translate, type Translate } from './i18n';
import { Badge, Button, Check, Empty, Field, Heading, Icon, Modal, Thumbnail } from './components';
import { VirtualList } from './VirtualList';
import { OptionsForm, QualitySelect, automaticQuality, commandOptions, defaults, encoders, optionLabel, qualityLabel } from './options';
import { bytes, dimensions, duration, frameRate, mediaQuality, mediaQualityWithFps, stageLabel, stepLabel, statusLabel, statusTone, taskActions, taskProgress } from './task';
import { SettingsPage } from './SettingsPage';
import { McpSettings } from './McpSettings';
import { AddDialog } from './AddDialog';
import { downloadLinks, singleVideoLink } from './links';
import { explainError } from './errors';
import { CoverStudio } from './CoverStudio';
import { TaskContextMenu } from './TaskContextMenu';
import { TaskLogsDialog } from './TaskLogs';
import type { AddReport, CollectionEntry, CollectionProbe, CoreEvent, DownloadOptions, Json, Media, MediaInfo, Page, ReleaseStatus, Session, Snapshot, Task, Tool } from './types';

export type Run = (operation: () => Promise<unknown>) => void;
type Dialog = { kind: 'options' } | { kind: 'add'; urls: string[]; options: DownloadOptions; entries: CollectionEntry[]; selectItems: boolean } | { kind: 'delete'; task: Task } | { kind: 'convert'; task: Task } | { kind: 'logs'; task: Task } | { kind: 'cover'; media: Media };
export function App() {
  const { state, dispatch } = useApp();
  const [authenticated, setAuthenticated] = useState<boolean | null>(null); const [error, setError] = useState('');
  const [message, setMessage] = useState(''); const [dialog, setDialog] = useState<Dialog | null>(null);
  const [adding, setAdding] = useState(''); const addingRef = useRef(false);
  const [release, setRelease] = useState<ReleaseStatus | null>(null); const [releaseBusy, setReleaseBusy] = useState(false); const [releaseError, setReleaseError] = useState('');
  const releaseChecked = useRef(false);
  const checkRelease = useCallback(async () => { setReleaseBusy(true); setReleaseError(''); try { setRelease(await host.releaseStatus()); } catch (reason) { setReleaseError(String(reason)); } finally { setReleaseBusy(false); } }, []);
  useEffect(() => { if (authenticated && !releaseChecked.current) { releaseChecked.current = true; void checkRelease(); } }, [authenticated, checkRelease]);
  const locale = localeFor(state.settings); const t = useCallback<Translate>((zh, en) => translate(locale, zh, en), [locale]);
  const settingsRef = useRef(state.settings); settingsRef.current = state.settings;
  const translateRef = useRef(t); translateRef.current = t;
  const run = useCallback<Run>(operation => { void operation().catch(reason => setError(reason instanceof Error ? reason.message : String(reason))); }, []);
  const navigate = useCallback((page: Page) => {
    if (!host.desktop) history.pushState(null, '', page === 'mcp' ? '/mcp' : '/');
    dispatch({ type: 'view', patch: { page } });
  }, [dispatch]);
  useEffect(() => { const changed = () => dispatch({ type: 'view', patch: { page: /^\/mcp\/?$/.test(location.pathname) ? 'mcp' : 'downloads' } }); window.addEventListener('popstate', changed); return () => window.removeEventListener('popstate', changed); }, [dispatch]);
  useEffect(() => { const expired = () => setAuthenticated(false); window.addEventListener('huifa-session-expired', expired); return () => window.removeEventListener('huifa-session-expired', expired); }, []);
  useEffect(() => { let active = true; void host.session().then(session => { if (active) setAuthenticated(session.authenticated); }).catch(reason => { if (active) { setError(String(reason)); setAuthenticated(false); } }); return () => { active = false; }; }, []);
  useEffect(() => {
    if (!authenticated) return;
    let disposed = false; let unlisten: (() => void) | undefined; let initialized = false; let toolsChecked = false; let refreshing: Promise<void> | null = null; const buffered: CoreEvent[] = []; const notified = new Set<string>();
    const refresh = (): Promise<void> => {
      if (refreshing) return refreshing;
      initialized = false;
      refreshing = host.command<Snapshot>('snapshot', { includeTools: false }).then(snapshot => {
        if (disposed) return;
        dispatch({ type: 'snapshot', snapshot });
        for (const task of snapshot.tasks) if (task.status === 'completed') notified.add(task.id);
        if (!toolsChecked) {
          toolsChecked = true;
          void host.command<Tool[]>('tools.check').then(tools => { if (!disposed) dispatch({ type: 'event', event: { kind: 'tools.updated', data: tools as unknown as Json } }); }).catch(() => { if (!disposed) setMessage(translateRef.current('未能检查运行时组件，可在“工具与更新”重试。', 'Runtime components could not be checked. Retry under Tools & updates.')); });
        }
      }).catch(reason => { if (!disposed) setError(String(reason)); }).finally(() => {
        if (!disposed) { for (const event of buffered.splice(0)) dispatch({ type: 'event', event }); initialized = true; }
        refreshing = null;
      });
      return refreshing;
    };
    void (async () => {
      unlisten = await host.subscribe(event => {
        if (disposed) return;
        if (event.kind === 'snapshot.required' || event.kind === 'host.resync') { void refresh(); return; }
        if (event.kind === 'host.error') { setError(String((event.data as { message: string }).message)); return; }
        if (!initialized) { buffered.push(event); return; }
        dispatch({ type: 'event', event });
        if (event.kind === 'task.upsert') { const task = event.data as unknown as Task; if (task.status === 'completed' && !notified.has(task.id)) { notified.add(task.id); if (settingsRef.current.desktopNotifications === true) run(() => host.notify(translateRef.current('下载完成', 'Download complete'), task.title)); } }
      }, connected => { if (!disposed) { dispatch({ type: 'connection', connected }); if (connected && initialized) void refresh(); } });
      if (disposed) { unlisten(); return; }
      await refresh();
    })().catch(reason => { if (!disposed) setError(String(reason)); });
    return () => { disposed = true; unlisten?.(); };
  }, [authenticated, dispatch]);
  useEffect(() => {
    const theme = String(state.settings.appearanceTheme || 'dark');
    const preference = matchMedia('(prefers-color-scheme: dark)');
    const apply = () => { document.documentElement.dataset.theme = theme === 'system' ? preference.matches ? 'dark' : 'light' : theme; };
    apply(); preference.addEventListener('change', apply); document.documentElement.lang = locale;
    return () => preference.removeEventListener('change', apply);
  }, [state.settings.appearanceTheme, locale]);
  useEffect(() => {
    const handle = (event: KeyboardEvent) => {
      if (dialog || !authenticated || !event.ctrlKey && !event.metaKey) return;
      if (/^[1-4]$/.test(event.key)) { event.preventDefault(); dispatch({ type: 'view', patch: { page: (['downloads', 'completed', 'settings', 'about'] as Page[])[Number(event.key) - 1] } }); }
      if (event.key.toLowerCase() === 'f') { const id = state.view.page === 'downloads' ? 'task-search' : state.view.page === 'completed' ? 'media-search' : ''; if (id) { event.preventDefault(); document.getElementById(id)?.focus(); } }
    };
    document.addEventListener('keydown', handle); return () => document.removeEventListener('keydown', handle);
  }, [dialog, authenticated, state.view.page, dispatch]);
  useEffect(() => { if (!message) return; const timer = setTimeout(() => setMessage(''), 5500); return () => clearTimeout(timer); }, [message]);
  const quality = state.view.quality;
  const options = useMemo(() => { const saved = { ...defaults, ...state.settings.downloadOptions }; return quality === null ? saved : { ...saved, ...automaticQuality(saved, quality) }; }, [state.settings.downloadOptions, quality]);
  const setQuality = useCallback((quality: string | null) => dispatch({ type: 'view', patch: { quality } }), [dispatch]);
  const add = useCallback((urls: string[], clipboard = false) => {
    if (addingRef.current) return;
    addingRef.current = true;
    setAdding(clipboard ? t('读取剪贴板…', 'Reading clipboard…') : t('识别链接…', 'Checking links…'));
    run(async () => {
      try {
        let text = urls.join('\n');
        if (clipboard) { try { text = await navigator.clipboard.readText(); } catch { throw new Error(t('无法读取剪贴板，请手动粘贴链接。', 'Cannot read the clipboard. Paste the links manually.')); } }
        const links = downloadLinks(text); const entries: CollectionEntry[] = []; let collection = false;
        for (const url of links) {
          if (singleVideoLink(url) || options.collectionMode === 'single') { entries.push({ url, title: url }); continue; }
          const result = await host.command<CollectionProbe>('probe.collection', { url, options: commandOptions(options) });
          collection ||= result.kind === 'collection' || !result.kind && result.entries?.length > 1;
          entries.push(...(result.entries || [{ url, title: result.title || url }]));
        }
        const distinct = [...new Map(entries.map(entry => [entry.url, entry])).values()];
        if (!distinct.length) throw new Error(t('没有符合条件的下载项目', 'No matching download items'));
        if (collection && options.collectionMode !== 'all' || options.contentMode === 'manual') { setDialog({ kind: 'add', urls: distinct.map(entry => entry.url), entries: distinct, options, selectItems: collection && options.collectionMode !== 'all' }); return; }
        setAdding(t('加入队列中…', 'Adding to queue…'));
        let completed = 0;
        const results: AddReport['results'] = [];
        try { for (const entry of distinct) { const report = await host.command<AddReport>('tasks.add', { urls: [entry.url], options: commandOptions({ ...options, collectionMode: 'single' }), report: true }); if (report?.results) results.push(...report.results); completed++; } }
        catch (reason) { dispatch({ type: 'view', patch: { urls: distinct.slice(completed).map(entry => entry.url).join('\n') } }); throw new Error(t(`已加入 ${completed} 项。${String(reason)}`, `Added ${completed} tasks. ${String(reason)}`)); }
        const existing = results.find(result => result.action === 'existing');
        dispatch({ type: 'view', patch: { urls: '', ...(existing ? { taskSelection: existing.id } : {}) } });
        setMessage(existing ? existing.status === 'paused' ? t('该任务已暂停，可在任务详情中继续。', 'This task is paused. Resume it from task details.') : t(`该任务已存在：${statusLabel(existing.status,t)}，不会重复下载。`, `This task already exists: ${statusLabel(existing.status,t)}. No duplicate was added.`) : t('任务已加入下载队列', 'Tasks added to the download queue'));
      } finally { addingRef.current = false; setAdding(''); }
    });
  }, [options, run, t, dispatch]);
  const selectTask = useCallback((id: string) => dispatch({ type: 'view', patch: { taskSelection: id } }), [dispatch]);
  const selectMedia = useCallback((id: string) => dispatch({ type: 'view', patch: { mediaSelection: id } }), [dispatch]);
  const taskAction = useCallback((task: Task, action: string) => {
    if (action === 'delete') { setDialog({ kind: 'delete', task }); return; }
    if (action === 'convert') { setDialog({ kind: 'convert', task }); return; }
    if (action === 'logs') { setDialog({ kind: 'logs', task }); return; }
    run(() => host.command(`tasks.${action}`, { id: task.id }));
  }, [run]);
  const cover = useCallback((media: Media) => setDialog({ kind: 'cover', media }), []);
  if (authenticated === null) return <div className="web-login"><Empty title={t('连接下载服务', 'Connecting to download service')} /></div>;
  if (!authenticated && host.desktop) return <div className="web-login"><div className="panel web-login-form"><h2>{t('下载服务未能启动', 'Download service could not start')}</h2><p className="error-text" role="alert">{explainError(error, t)}</p><Button tone="primary" onClick={() => run(async () => { await host.session(); setError(''); setAuthenticated(true); })}>{t('重试连接', 'Retry connection')}</Button></div></div>;
  if (!authenticated) return <Login t={t} error={error} run={run} connected={() => { setError(''); setAuthenticated(true); }} />;
  const activeCount = state.activeTaskCount;
  return <>
    <div className="app-shell"><nav className="navigation" aria-label={t('导航', 'Navigation')}>
      <div className="brand"><div className="brand-mark"><Icon name="download" /></div><div><strong>Huifa</strong><small>MEDIA WORKSPACE</small></div></div>
      <div className="nav-links">{([['downloads', '下载任务', 'Downloads', 'download'], ['completed', '已完成', 'Completed', 'library'], ['mcp', 'AI 媒体服务（MCP）', 'AI media service (MCP)', 'video'], ['settings', '设置', 'Settings', 'settings'], ['about', '关于', 'About', 'info']] as const).map(([page, zh, en, icon]) => <button key={page} className={`nav-link ${state.view.page === page ? 'active' : ''}`} aria-current={state.view.page === page ? 'page' : undefined} title={t(zh, en)} aria-label={t(zh, en)} onClick={() => navigate(page)}><Icon name={icon} /><span>{page === 'mcp' ? t('AI 媒体服务', 'AI media service') : t(zh, en)}</span></button>)}</div>
      {state.view.page === 'downloads' && <div className="nav-categories"><small className="category-label">{t('任务分类', 'Task categories')}</small>{['all', 'active', 'queued', 'waiting_processing', 'processing', 'paused', 'completed', 'processing_failed', 'failed'].map(status => <button key={status} className={`nav-link category-link ${state.view.taskStatus === status ? 'active' : ''}`} aria-pressed={state.view.taskStatus === status} title={status === 'all' ? t('全部任务', 'All tasks') : status === 'active' ? t('进行中', 'Active') : statusLabel(status, t)} onClick={() => dispatch({ type: 'view', patch: { taskStatus: status } })}><Icon name={status === 'all' ? 'download' : status === 'completed' ? 'check' : status === 'paused' ? 'pause' : status.includes('failed') ? 'info' : 'play'} /><span>{status === 'all' ? t('全部任务', 'All tasks') : status === 'active' ? t('进行中', 'Active') : statusLabel(status, t)}</span></button>)}</div>}
      <div className="nav-bottom"><span className={`connection-dot ${state.connected ? '' : 'offline'}`} /><span>{state.connected ? t('服务已连接', 'Service connected') : t('连接恢复中', 'Reconnecting')}</span><button className="version-link" title={release?.updateAvailable ? t('打开新版下载页面', 'Open new release page') : t('版本与更新', 'Version and updates')} onClick={() => release?.updateAvailable ? run(() => host.openRelease(release.url)) : navigate('about')}>v{state.version}{release?.updateAvailable && <span className="badge">{t('有新版', 'Update')}</span>}</button></div>
    </nav><main className="main" id="main-content">
      {!state.loaded ? <Empty title={t('载入任务、媒体和设置…', 'Loading tasks, media, and settings…')} /> : state.view.page === 'downloads' ? <Downloads adding={adding} clipboard={() => add([], true)} state={state} dispatch={dispatch} options={options} changeQuality={setQuality} add={add} select={selectTask} action={taskAction} run={run} quick={() => setDialog({ kind: 'options' })} t={t} /> : state.view.page === 'completed' ? <Completed state={state} dispatch={dispatch} select={selectMedia} action={taskAction} cover={cover} run={run} t={t} /> : state.view.page === 'mcp' ? <div className="page mcp-workspace page-scroll"><McpSettings run={run} t={t} /></div> : state.view.page === 'settings' ? <SettingsPage state={state} dispatch={dispatch} t={t} run={run} message={setMessage} /> : <About version={state.version} release={release} busy={releaseBusy} error={releaseError} check={checkRelease} t={t} run={run} />}
    </main></div>
    <footer className="statusbar"><span>{t(`${state.taskIds.length} 项任务 · ${activeCount} 项活动`, `${state.taskIds.length} tasks · ${activeCount} active`)}</span><span className="status-speed" aria-label={t('总下载速度', 'Total download speed')}>{t('总下载速度', 'Total download speed')}：{state.connected ? state.downloadSpeed > 0 ? `${bytes(state.downloadSpeed)}/s` : '0 B/s' : '—'}</span><span className="status-message">{message || (!state.connected ? t('连接恢复后自动同步任务。', 'Tasks sync when the connection returns.') : t('就绪', 'Ready'))}</span><span className="status-shortcut">{t('Ctrl+1–4 切换页面', 'Ctrl+1–4 switch pages')}</span>{!host.desktop && <Button tone="text" onClick={() => run(async () => { await host.logout(); setAuthenticated(false); })}>{t('退出登录', 'Sign out')}</Button>}</footer>
    {(error || message) && <div className="toasts" aria-live="polite"><div className={`toast ${error ? 'danger' : ''}`}><span role={error ? 'alert' : 'status'}>{error ? explainError(error, t) : message}</span><Button icon="close" tone="icon-only" aria-label={t('关闭提示', 'Dismiss message')} onClick={() => { setError(''); setMessage(''); }} /></div></div>}
    {dialog?.kind === 'add' && <AddDialog urls={dialog.urls} options={dialog.options} initialEntries={dialog.entries} selectItems={dialog.selectItems} t={t} run={run} close={() => setDialog(null)} added={report => { const existing = report?.results.find(result => result.action === 'existing'); dispatch({ type: 'view', patch: { urls: '', ...(existing ? { taskSelection: existing.id } : {}) } }); setDialog(null); setMessage(existing ? existing.status === 'paused' ? t('该任务已暂停，可在任务详情中继续。', 'This task is paused. Resume it from task details.') : t(`该任务已存在：${statusLabel(existing.status,t)}，不会重复下载。`, `This task already exists: ${statusLabel(existing.status,t)}. No duplicate was added.`) : t('任务已加入下载队列', 'Tasks added to the download queue')); }} />}
    {dialog?.kind === 'options' && <QuickOptions initial={options} t={t} close={() => setDialog(null)} apply={async next => { const settings = await host.command<Snapshot['settings']>('settings.save', { settings: { ...state.settings, downloadOptions: commandOptions(next) } }); dispatch({ type: 'event', event: { kind: 'settings.updated', data: settings } }); setQuality(null); setDialog(null); }} />}
    {dialog?.kind === 'delete' && <DeleteDialog task={dialog.task} initialFiles={state.settings.uiPreferences?.deleteMediaOnTaskDelete === true} remember={async files => { const settings = await host.command<Snapshot['settings']>('settings.preferences', { preferences: { deleteMediaOnTaskDelete: files } }); dispatch({ type: 'event', event: { kind: 'settings.updated', data: settings } }); }} t={t} run={run} removed={snapshot => dispatch({ type: 'snapshot', snapshot })} close={() => setDialog(null)} />}
    {dialog?.kind === 'convert' && <ConvertDialog task={dialog.task} t={t} run={run} close={() => setDialog(null)} />}
    {dialog?.kind === 'cover' && <CoverStudio media={dialog.media} t={t} run={run} close={() => setDialog(null)} />}
    {dialog?.kind === 'logs' && <TaskLogsDialog task={state.tasks[dialog.task.id] || dialog.task} t={t} close={() => setDialog(null)} />}
  </>;
}
function Login({ t, error, connected, run }: { t: Translate; error: string; connected: () => void; run: Run }) {
  const [account, setAccount] = useState(''); const [password, setPassword] = useState(''); const [busy, setBusy] = useState(false);
  return <div className="web-login"><form className="panel web-login-form" onSubmit={event => { event.preventDefault(); setBusy(true); run(async () => { try { await host.login(account, password); setPassword(''); connected(); } finally { setBusy(false); } }); }}>
    <div className="brand"><div className="brand-mark"><Icon name="download" /></div><div><strong>Huifa</strong><small>MEDIA DOWNLOADER</small></div></div><h2>{t('登录下载工作空间', 'Sign in to your download workspace')}</h2>
    <Field label={t('账号', 'Account')}><input autoFocus value={account} onChange={event => setAccount(event.target.value)} autoComplete="username" required maxLength={128} /></Field>
    <Field label={t('密码', 'Password')}><input type="password" value={password} onChange={event => setPassword(event.target.value)} autoComplete="current-password" required maxLength={1024} /></Field>
    {error && <p className="error-text" role="alert">{explainError(error, t)}</p>}<Button type="submit" tone="primary" disabled={busy}>{busy ? t('连接中…', 'Connecting…') : t('登录', 'Sign in')}</Button>
  </form></div>;
}
const Composer = memo(function Composer({ urls, options, change, changeQuality, add, quick, t, adding, clipboard }: { urls: string; options: DownloadOptions; change: (value: string) => void; changeQuality: (quality: string) => void; add: (urls: string[]) => void; quick: () => void; t: Translate; adding: string; clipboard: () => void }) {
  return <form className="composer panel" onSubmit={event => { event.preventDefault(); const links = urls.split(/\s+/).map(value => value.trim()).filter(Boolean); if (links.length) add([...new Set(links)]); }}>
    <div className="composer-title"><strong>{t('新建下载', 'New download')}</strong><span className="muted">{t('支持单条、合集和批量链接', 'Single links, collections, and batches')}</span><Button icon="settings" size="small" tone="subtle" onClick={quick}>{t('快速选项', 'Quick options')}</Button></div>
    <div className="composer-input"><textarea aria-label={t('下载链接', 'Download links')} rows={2} value={urls} placeholder={t('粘贴视频或合集链接，每行一个…', 'Paste video or collection links, one per line…')} onChange={event => change(event.target.value)} onKeyDown={event => { if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') { event.preventDefault(); event.currentTarget.form?.requestSubmit(); } }} /><Button icon="copy" disabled={Boolean(adding)} onClick={clipboard}>{t('剪贴板下载', 'Download clipboard')}</Button><Button icon="plus" type="submit" tone="primary" disabled={Boolean(adding) || !urls.trim()}>{adding || t('添加下载', 'Add downloads')}</Button></div>
    <div className="batch-plan"><Badge>{t('此批次计划', 'Batch plan')}</Badge><label className="quality-picker">{t('画质', 'Quality')}<QualitySelect options={options} change={changeQuality} t={t} /></label><span>{options.contentMode === 'audio' ? t('仅音频', 'Audio only') : options.contentMode === 'manual' ? t('手动选格式', 'Choose format') : t('视频与音频', 'Video with audio')} · {optionLabel(String(options.encoder || 'copy'), t)} · {optionLabel(String(options.subtitleLanguage || 'none'), t)}</span><small>{t('加入后方案固定；设置更改仅影响新任务', 'The plan is fixed when added; settings affect new tasks only')}</small></div>
  </form>;
});
function Progress({ task }: { task: Task }) { const progress = taskProgress(task); return <div className={`progress ${progress.indeterminate ? 'indeterminate' : ''}`} role="progressbar" aria-label={task.stage || task.status} aria-valuemin={0} aria-valuemax={100} aria-valuenow={progress.indeterminate ? undefined : progress.value}><span style={{ width: `${progress.value}%` }} /></div>; }
const TaskRow = memo(function TaskRow({ task, mediaId, t }: { task: Task; mediaId?: string; t: Translate }) {
  const progress = taskProgress(task);
  const quality = task.status === 'completed' ? mediaQualityWithFps(task.mediaInfo) : '—';
  return <div className="task-row-content task-columns" title={task.title || task.url}><div className="task-name"><Thumbnail url={task.hasThumbnail ? host.assetUrl(task.id, 'taskThumbnail') : mediaId ? host.assetUrl(mediaId, 'thumbnail') : undefined} /><div className="row-main"><strong className="truncate">{task.title || t('等待获取媒体信息', 'Waiting for media information')}</strong><small className={`task-status ${statusTone(task.status)}`}>{statusLabel(task.status, t)}</small></div></div><div className="task-stage"><div className="task-stage-label"><span className="truncate" title={quality === '—' ? undefined : `${quality} · ${dimensions(task.mediaInfo)}`}>{stepLabel(task)}{stageLabel(task.stage, task.status, t)}{quality !== '—' && <> · {quality}</>}</span><small>{progress.indeterminate ? '—' : `${progress.value.toFixed(1)}%`}</small></div><Progress task={task} /></div><span className="task-speed truncate">{typeof task.speed === 'number' ? `${bytes(task.speed)}/s` : task.speed || '—'}</span><span className="task-eta truncate">{duration(task.eta)}</span><span className="task-size truncate">{bytes(task.size ?? task.mediaInfo?.size)}</span></div>;
});
function Downloads({ state, dispatch, options, changeQuality, add, select, action, quick, run, t, adding, clipboard }: { state: AppState; dispatch: Dispatch<Action>; options: DownloadOptions; changeQuality: (quality: string) => void; add: (urls: string[]) => void; select: (id: string) => void; action: (task: Task, action: string) => void; quick: () => void; run: Run; t: Translate; adding: string; clipboard: () => void }) {
  const { view, tasks } = state;
  const [context, setContext] = useState<{ id: string; position: { x: number; y: number }; trigger: HTMLElement } | null>(null);
  const closeContext = useCallback(() => setContext(null), []);
  const openContext = useCallback((id: string, position: { x: number; y: number }, trigger: HTMLElement) => setContext({ id, position, trigger }), []);
  // Progress, speed and ETA do not change list membership or order.
  const ids = useMemo(() => {
    const search = view.taskSearch.toLowerCase();
    return state.taskIds.filter(id => { const task = tasks[id]; return (!search || `${task.title} ${task.url} ${id}`.toLowerCase().includes(search)) && (view.taskStatus === 'all' || view.taskStatus === 'active' ? view.taskStatus !== 'active' || ['queued', 'probing', 'downloading', 'waiting_processing', 'processing'].includes(task.status) : task.status === view.taskStatus); }).sort((a, b) => view.taskSort === 'title' ? tasks[a].title.localeCompare(tasks[b].title) : view.taskSort === 'oldest' ? tasks[a].createdAt.localeCompare(tasks[b].createdAt) : tasks[b].createdAt.localeCompare(tasks[a].createdAt));
  }, [state.taskIds, state.taskListVersion, view.taskSearch, view.taskStatus, view.taskSort]);
  const selected = view.taskSelection || ids[0] || '';
  useEffect(() => { if (context && !ids.includes(context.id)) closeContext(); }, [context, ids, closeContext]);
  const hidden = useMemo(() => !ids.includes(selected), [ids, selected]);
  const row = useCallback((id: string) => <TaskRow task={tasks[id]} mediaId={state.mediaByTask[id]} t={t} />, [tasks, state.mediaByTask, t]);
  const urlsChange = useCallback((urls: string) => dispatch({ type: 'view', patch: { urls } }), [dispatch]);
  return <div className="page downloads-page"><Heading title={t('下载任务', 'Downloads')} subtitle={t('下载与处理独立排队，原始媒体先保存到本地。', 'Downloads and processing have separate queues. Source media is saved first.')}>{host.desktop && <Button icon="folder" onClick={() => run(() => host.openDownloadDirectory())}>{t('下载目录', 'Download folder')}</Button>}</Heading>
    <Composer adding={adding} clipboard={clipboard} urls={view.urls} options={options} change={urlsChange} changeQuality={changeQuality} add={add} quick={quick} t={t} />
    <div className="toolbar"><input id="task-search" aria-label={t('搜索任务', 'Search tasks')} type="search" value={view.taskSearch} placeholder={t('搜索所有任务：标题、链接、编号', 'Search all tasks: title, URL, ID')} onChange={event => dispatch({ type: 'view', patch: { taskSearch: event.target.value } })} /><select aria-label={t('状态筛选', 'Status filter')} value={view.taskStatus} onChange={event => dispatch({ type: 'view', patch: { taskStatus: event.target.value } })}>{['all', 'active', 'queued', 'probing', 'downloading', 'waiting_processing', 'processing', 'paused', 'completed', 'processing_failed', 'failed', 'cancelled'].map(status => <option key={status} value={status}>{status === 'all' ? t('全部状态', 'All statuses') : status === 'active' ? t('进行中', 'Active') : statusLabel(status, t)}</option>)}</select><select aria-label={t('排序', 'Sort')} value={view.taskSort} onChange={event => dispatch({ type: 'view', patch: { taskSort: event.target.value } })}><option value="newest">{t('最新优先', 'Newest first')}</option><option value="oldest">{t('最早优先', 'Oldest first')}</option><option value="title">{t('标题排序', 'Title')}</option></select><Button icon="pause" title={t('暂停全部', 'Pause all')} aria-label={t('暂停全部', 'Pause all')} tone="icon-only" onClick={() => run(() => host.command('tasks.pauseAll'))} /><Button icon="play" title={t('继续全部', 'Resume all')} aria-label={t('继续全部', 'Resume all')} tone="icon-only" onClick={() => run(() => host.command('tasks.resumeAll'))} /></div>
    <div className="workspace"><section className="list-panel panel"><div className="list-caption"><strong>{t('任务', 'Tasks')}</strong><span>{ids.length} / {state.taskIds.length}</span></div><div className="task-table-heading task-columns"><span>{t('名称', 'Name')}</span><span>{t('阶段 / 进度', 'Stage / progress')}</span><span className="task-speed">{t('速度', 'Speed')}</span><span className="task-eta">{t('剩余', 'ETA')}</span><span className="task-size">{t('大小', 'Size')}</span></div>{ids.length ? <VirtualList ids={ids} selected={selected} onSelect={select} onContextMenu={openContext} renderRow={row} label={t('下载任务列表', 'Download task list')} scrollKey="tasks" /> : <Empty title={t('没有匹配的任务', 'No matching tasks')} detail={view.taskSearch || view.taskStatus !== 'all' ? t('调整搜索或状态筛选。', 'Adjust search or status filters.') : t('在上方粘贴链接开始下载。', 'Paste a link above to start downloading.')} />}</section><TaskDetail task={tasks[selected]} media={state.media[state.mediaByTask[selected]]} hidden={Boolean(tasks[selected]) && hidden} action={action} run={run} t={t} dispatch={dispatch} /></div>
    {context && tasks[context.id] && <TaskContextMenu task={tasks[context.id]} media={state.media[state.mediaByTask[context.id]]} position={context.position} trigger={context.trigger} close={closeContext} action={action} run={run} t={t} />}
  </div>;
}
const TaskDetail = memo(function TaskDetail({ task, media, hidden, action, run, t, dispatch }: { task?: Task; media?: Media; hidden: boolean; action: (task: Task, action: string) => void; run: Run; t: Translate; dispatch: Dispatch<Action> }) {
  const [infoState, setInfoState] = useState({ id: '', busy: false, error: '' });
  const [infoRetry, setInfoRetry] = useState(0);
  const infoRequest = useRef<{ id: string; promise: Promise<Task> } | null>(null);
  const info = task?.mediaInfo || media?.mediaInfo;
  useEffect(() => {
    if (!task || task.status !== 'completed' || task.kind === 'assets' || info) return;
    const id = task.id; let disposed = false;
    setInfoState({ id, busy: true, error: '' });
    if (infoRequest.current?.id !== id) infoRequest.current = { id, promise: host.command<Task>('tasks.info', { id }) };
    void infoRequest.current.promise.then(result => { if (!disposed) { dispatch({ type: 'event', event: { kind: 'task.upsert', data: result as unknown as Json } }); setInfoState({ id, busy: false, error: '' }); } }).catch(reason => { if (!disposed) setInfoState({ id, busy: false, error: String(reason) }); });
    return () => { disposed = true; };
  }, [task?.id, task?.status, info, infoRetry, dispatch]);
  if (!task) return <aside className="detail panel"><Empty title={t('任务详情', 'Task details')} detail={t('选中任务以查看计划、进度和操作。', 'Select a task to see its plan, progress, and actions.')} icon="info" /></aside>;
  const progress = taskProgress(task);
  const controls = taskActions(task, t);
  return <aside className="detail panel" aria-label={t('任务详情', 'Task details')}><div className="detail-scroll"><div className="detail-top"><span className="eyebrow">{t('已选任务', 'Selected task')}</span><Badge tone={statusTone(task.status)}>{statusLabel(task.status, t)}</Badge></div><Thumbnail large url={task.hasThumbnail ? host.assetUrl(task.id, 'taskThumbnail') : media && host.assetUrl(media.id, 'thumbnail')} /><h2>{task.title || t('等待媒体信息', 'Waiting for media information')}</h2><code className="task-id">{task.id}</code>{hidden && <p className="muted">{t('所选任务被当前筛选隐藏。', 'Selected task is hidden by the current filter.')}</p>}
    <div className="detail-stage"><strong>{stepLabel(task)}{stageLabel(task.stage, task.status, t)}</strong><span>{progress.indeterminate ? t('等待阶段进度', 'Waiting for stage progress') : `${progress.value.toFixed(1)}%`}</span></div><Progress task={task} />{task.error && <p className="error-text" role="alert">{explainError(task.error, t)}</p>}{task.warnings?.map(warning => <p key={warning} className="notice" role="status">{explainError(warning, t)}</p>)}
    {task.kind === 'assets' && <p className="muted">{t('附件保存任务', 'Attachment task')}</p>}{Boolean(task.files?.length) && <div className="task-files"><h3>{t('生成文件', 'Generated files')}</h3>{task.files?.map(file => <p key={file.filename} className="break-text">{file.filename}<small className="muted"> · {file.kind} · {(file.size / 1024).toFixed(1)} KiB</small></p>)}</div>}
    <div className="detail-actions">{controls.length > 0 && <div className="detail-task-controls">{controls.map(item => <Button key={item.action} icon={item.icon} tone={['resume', 'retry'].includes(item.action) ? 'primary' : ''} onClick={() => action(task, item.action)}>{item.label}</Button>)}</div>}<Button icon="info" onClick={() => action(task, 'logs')}>{t('查看日志', 'View logs')}</Button></div>
    <dl className="facts"><dt>{t('创建时间', 'Created')}</dt><dd>{new Date(task.createdAt).toLocaleString()}</dd><dt>{t('处理计划', 'Processing plan')}</dt><dd>{qualityLabel(task.options, t)} · {optionLabel(String(task.options.encoder || 'copy'), t)} · {optionLabel(String(task.options.subtitleLanguage || 'none'), t)}</dd><dt>{t('来源', 'Source')}</dt><dd className="break-text">{task.url}</dd><dt>{t('结果文件', 'Result file')}</dt><dd className="break-text">{task.outputPath || t('尚未保存', 'Not saved yet')}</dd></dl>
    {task.kind !== 'assets' && <><MediaInformation title={t('来源视频信息', 'Source video information')} info={task.sourceInfo} t={t} /><MediaInformation title={t('结果文件信息', 'Result file information')} info={info} size={task.size ?? media?.size} t={t} />{!info && infoState.id === task.id && infoState.busy && <p className="hint">{t('正在读取本地文件信息…', 'Reading local file information…')}</p>}{!info && infoState.id === task.id && infoState.error && <p className="hint" title={explainError(infoState.error, t)}>{t('暂时无法读取结果信息，原文件未修改。', 'Result information could not be read. The file is unchanged.')} <Button size="small" onClick={() => { infoRequest.current = null; setInfoRetry(value => value + 1); }}>{t('重试', 'Retry')}</Button></p>}</>}
    <details><summary>{t('更多操作', 'More actions')}</summary><div className="actions wrap">{task.status === 'completed' && media && <Button icon="refresh" size="small" onClick={() => action(task, 'convert')}>{t('转换格式', 'Convert format')}</Button>}<Button icon="copy" size="small" onClick={() => run(() => host.copyTask(task, 'title'))}>{t('复制视频名', 'Copy video title')}</Button><Button icon="copy" size="small" onClick={() => run(() => host.copyTask(task, 'source'))}>{t('复制来源', 'Copy source')}</Button>{host.desktop && <Button icon="copy" size="small" disabled={!media} onClick={() => run(() => host.copyTask(task, 'path'))}>{t('复制完整路径', 'Copy full path')}</Button>}<Button icon="trash" size="small" tone="danger" onClick={() => action(task, 'delete')}>{t('删除任务', 'Delete task')}</Button></div></details></div>
    <div className="detail-footer"><Button icon={host.desktop ? 'folder' : 'download'} tone="primary" disabled={!media} onClick={() => media && run(() => host.result(media.id, host.desktop ? 'reveal' : 'download'))}>{host.desktop ? t('定位结果文件', 'Reveal result file') : t('下载结果文件', 'Download result file')}</Button></div>
  </aside>;
});
function MediaInformation({ title, info, size, t }: { title: string; info?: MediaInfo | null; size?: number | string | null; t: Translate }) {
  const fileSize = size ?? info?.size;
  return <section className="media-information" aria-label={title}><h3>{title}</h3>{info || fileSize ? <dl className="facts"><dt>{t('实际画质 / 尺寸', 'Actual quality / dimensions')}</dt><dd>{mediaQuality(info)} · {dimensions(info)}</dd><dt>{t('帧率', 'Frame rate')}</dt><dd>{frameRate(info?.fps)}</dd><dt>{t('视频 / 音频编码', 'Video / audio codecs')}</dt><dd>{info?.videoCodec || '—'} / {info?.audioCodec || '—'}</dd><dt>{t('容器格式', 'Container')}</dt><dd>{info?.container || '—'}</dd><dt>{t('大小 / 时长', 'Size / duration')}</dt><dd>{bytes(fileSize)} · {duration(info?.duration)}</dd></dl> : <p className="hint">{t('暂无可用信息', 'No information available')}</p>}</section>;
}
function Completed({ state, dispatch, select, action, cover, run, t }: { state: AppState; dispatch: Dispatch<Action>; select: (id: string) => void; action: (task: Task, action: string) => void; cover: (media: Media) => void; run: Run; t: Translate }) {
  const ids = useMemo(() => state.mediaIds.filter(id => `${state.media[id].title} ${state.media[id].filename}`.toLowerCase().includes(state.view.mediaSearch.toLowerCase())).reverse(), [state.mediaIds, state.media, state.view.mediaSearch]);
  const selected = state.view.mediaSelection || ids[0]; const item = state.media[selected];
  const [infoState, setInfoState] = useState({ id: '', busy: false, error: '' });
  const [infoRetry, setInfoRetry] = useState(0);
  const [context, setContext] = useState<{ id: string; task: Task; position: { x: number; y: number }; trigger: HTMLElement } | null>(null);
  const contextRequest = useRef(0);
  const closeContext = useCallback(() => { contextRequest.current++; setContext(null); }, []);
  const openContext = useCallback((id: string, position: { x: number; y: number }, trigger: HTMLElement) => {
    const media = state.media[id]; const task = state.tasks[media.taskId];
    const request = ++contextRequest.current;
    setContext(null);
    if (task) { setContext({ id, task, position, trigger }); return; }
    run(async () => { const task = await host.command<Task>('tasks.get', { id: media.taskId }); if (request === contextRequest.current) setContext({ id, task, position, trigger }); });
  }, [state.media, state.tasks, run]);
  useEffect(() => { if (context && !ids.includes(context.id)) closeContext(); }, [context, ids, closeContext]);
  useEffect(() => () => { contextRequest.current++; }, []);
  const infoRequest = useRef<{ id: string; promise: Promise<Task> } | null>(null);
  useEffect(() => {
    if (!item || item.mediaInfo) return;
    const id = item.taskId; let disposed = false;
    setInfoState({ id, busy: true, error: '' });
    if (infoRequest.current?.id !== id) infoRequest.current = { id, promise: host.command<Task>('tasks.info', { id }) };
    void infoRequest.current.promise.then(result => { if (!disposed) { if (result.mediaInfo) dispatch({ type: 'event', event: { kind: 'media.upsert', data: { id: item.id, mediaInfo: result.mediaInfo } } }); setInfoState({ id, busy: false, error: '' }); } }).catch(reason => { if (!disposed) setInfoState({ id, busy: false, error: String(reason) }); });
    return () => { disposed = true; };
  }, [item?.id, item?.taskId, item?.mediaInfo, infoRetry, dispatch]);
  const row = useCallback((id: string) => <MediaRow media={state.media[id]} t={t} />, [state.media, t]);
  const contextMenu = context && state.media[context.id] && <TaskContextMenu task={state.tasks[context.task.id] || context.task} media={state.media[context.id]} position={context.position} trigger={context.trigger} close={closeContext} action={action} run={run} t={t} />;
  return <div className="page"><Heading title={t('已完成', 'Completed media')} subtitle={t(`完整媒体库 · ${state.mediaIds.length} 个媒体`, `Full media library · ${state.mediaIds.length} media`)}><Button icon="refresh" onClick={() => run(async () => { const snapshot = await host.command<Snapshot>('snapshot'); dispatch({ type: 'snapshot', snapshot }); })}>{t('刷新', 'Refresh')}</Button></Heading><div className="toolbar"><input id="media-search" type="search" aria-label={t('搜索媒体库', 'Search media library')} value={state.view.mediaSearch} placeholder={t('搜索标题、文件名', 'Search title and filename')} onChange={event => dispatch({ type: 'view', patch: { mediaSearch: event.target.value } })} /></div><div className="workspace"><section className="panel list-panel"><div className="list-caption"><strong>{t('媒体库', 'Media library')}</strong><span>{ids.length} / {state.mediaIds.length}</span></div>{ids.length ? <VirtualList ids={ids} selected={selected} onSelect={select} onContextMenu={openContext} renderRow={row} label={t('已完成媒体列表', 'Completed media list')} scrollKey="media" /> : <Empty title={t('没有匹配的媒体', 'No matching media')} detail={t('下载完成的媒体可在这里查看和准备封面。', 'View completed downloads and prepare covers here.')} icon="library" />}</section><aside className="detail panel">{item ? <><div className="detail-scroll"><span className="eyebrow">{t('已选媒体', 'Selected media')}</span><Thumbnail large url={host.assetUrl(item.id, 'thumbnail')} /><h2>{item.title}</h2><dl className="facts"><dt>{t('结果文件', 'Result file')}</dt><dd className="break-text">{item.filename}</dd><dt>{t('大小 / 时长', 'Size / duration')}</dt><dd>{bytes(item.size)} · {duration(item.duration)}</dd></dl><MediaInformation title={t('结果文件信息', 'Result file information')} info={item.mediaInfo} size={item.size} t={t} />{!item.mediaInfo && infoState.id === item.taskId && infoState.busy && <p className="hint">{t('正在读取本地文件信息…', 'Reading local file information…')}</p>}{!item.mediaInfo && infoState.id === item.taskId && infoState.error && <p className="hint" title={explainError(infoState.error, t)}>{t('暂时无法读取结果信息，原文件未修改。', 'Result information could not be read. The file is unchanged.')} <Button size="small" onClick={() => { infoRequest.current = null; setInfoRetry(value => value + 1); }}>{t('重试', 'Retry')}</Button></p>}<div className="actions wrap"><Button icon="image" onClick={() => cover(item)}>{t('封面工作室', 'Cover studio')}</Button>{host.desktop && <Button icon="play" onClick={() => run(() => host.result(item.id, 'open'))}>{t('打开媒体', 'Open media')}</Button>}</div></div><div className="detail-footer"><Button tone="primary" icon={host.desktop ? 'folder' : 'download'} onClick={() => run(() => host.result(item.id, host.desktop ? 'reveal' : 'download'))}>{host.desktop ? t('定位结果文件', 'Reveal result file') : t('下载结果文件', 'Download result file')}</Button></div></> : <Empty title={t('媒体详情', 'Media details')} detail={t('选择媒体查看文件操作。', 'Select media to view file actions.')} icon="library" />}</aside></div>{contextMenu}</div>;
}
const MediaRow = memo(function MediaRow({ media, t }: { media: Media; t: Translate }) { const quality = mediaQualityWithFps(media.mediaInfo); return <div className="media-row-content"><Thumbnail url={host.assetUrl(media.id, 'thumbnail')} /><div className="row-main"><div className="row-heading"><strong className="truncate">{media.title}</strong><Badge tone="success">{t('已完成', 'Completed')}{quality !== '—' && <> · {quality}</>}</Badge></div><div className="row-meta"><span className="truncate">{media.filename}</span><span>{bytes(media.size)} · {duration(media.duration)}</span></div></div></div>; });
function QuickOptions({ initial, apply, close, t }: { initial: DownloadOptions; apply: (value: DownloadOptions) => Promise<void>; close: () => void; t: Translate }) { const [options, setOptions] = useState(initial); const [busy, setBusy] = useState(false); const [error, setError] = useState(''); const change = useCallback((key: string, value: DownloadOptions[string]) => setOptions(current => ({ ...current, [key]: value })), []); return <Modal title={t('下一批次下载方案', 'Next batch download plan')} subtitle={t('已加入任务的方案已固定。', 'Existing tasks keep their saved plans.')} t={t} close={() => { if (!busy) close(); }} footer={<><Button disabled={busy} onClick={close}>{t('取消', 'Cancel')}</Button><Button tone="primary" disabled={busy} onClick={() => { setBusy(true); void apply(options).catch(reason => setError(String(reason))).finally(() => setBusy(false)); }}>{busy ? t('保存中…', 'Saving…') : t('保存批次方案', 'Save batch plan')}</Button></>}>{error && <p className="notice danger" role="alert">{explainError(error, t)}</p>}<OptionsForm options={options} change={change} t={t} quick /></Modal>; }
function DeleteDialog({ task, initialFiles, remember, close, removed, run, t }: { task: Task; initialFiles: boolean; remember: (files: boolean) => Promise<void>; close: () => void; removed: (snapshot: Snapshot) => void; run: Run; t: Translate }) {
  const [files, setFiles] = useState(initialFiles); const [busy, setBusy] = useState(false); const [saving, setSaving] = useState(false); const [error, setError] = useState('');
  const changeFiles = (next: boolean) => {
    setFiles(next); setSaving(true); setError('');
    void remember(next).catch(reason => { setFiles(!next); setError(String(reason)); }).finally(() => setSaving(false));
  };
  return <Modal title={t('删除任务', 'Delete task')} t={t} close={() => { if (!busy && !saving) close(); }} footer={<><Button onClick={close} disabled={busy || saving}>{t('取消', 'Cancel')}</Button><Button tone="danger" disabled={busy || saving} onClick={() => { setBusy(true); run(async () => { try { await host.command('tasks.delete', { id: task.id, deleteFiles: files }); removed(await host.command<Snapshot>('snapshot')); close(); } finally { setBusy(false); } }); }}>{busy ? t('删除中…', 'Deleting…') : t('删除', 'Delete')}</Button></>}>
    <p className="dialog-copy">{t(`删除“${task.title || task.id}”？正在运行的任务会先停止。`, `Delete “${task.title || task.id}”? Active work stops first.`)}</p>
    <Check label={t('同时删除此任务管理的媒体文件', 'Also delete media files managed by this task')} checked={files} disabled={busy || saving} onChange={event => changeFiles(event.target.checked)} />
    <p className="hint">{t('自动记住此选择。仅删除记录会保留媒体。', 'This choice is remembered automatically. Deleting only the record keeps the media.')}</p>
    {error && <p className="notice danger" role="alert">{explainError(error, t)}</p>}
  </Modal>;
}
function ConvertDialog({ task, close, run, t }: { task: Task; close: () => void; run: Run; t: Translate }) {
  const [container,setContainer]=useState('mp4'); const [strategy,setStrategy]=useState('auto'); const [encoder,setEncoder]=useState('libx264');
  const [busy,setBusy]=useState(false); const [preview,setPreview]=useState<{strategy:string;encoder:string;audioEncoder:string;reason:string}|null>(null); const [error,setError]=useState('');
  const options=useMemo(()=>({container,...(strategy==='transcode'?{encoder}: {})}),[container,strategy,encoder]);
  useEffect(()=>{let active=true; setPreview(null);setError('');void host.command<typeof preview>('tasks.convertPreview',{id:task.id,options,strategy}).then(value=>{if(active)setPreview(value);}).catch(reason=>{if(active)setError(String(reason));});return()=>{active=false;};},[task.id,options,strategy]);
  return <Modal title={t('转换视频格式', 'Convert video format')} subtitle={task.title} close={()=>{if(!busy)close();}} t={t} footer={<><Button onClick={close} disabled={busy}>{t('取消', 'Cancel')}</Button><Button tone="primary" disabled={busy||!preview||Boolean(error)} onClick={()=>{setBusy(true);run(async()=>{try{await host.command('tasks.convert',{id:task.id,options,strategy});close();}finally{setBusy(false);}});}}>{busy?t('加入队列中…','Adding to queue…'):t('开始转换','Start conversion')}</Button></>}>
    <Field label={t('目标格式','Target format')}><select value={container} onChange={event=>setContainer(event.target.value)}>{['mp4','mkv','mov','webm'].map(value=><option key={value} value={value}>{value.toUpperCase()}</option>)}</select></Field>
    <p className="hint">{t('默认自动选择：能直接换封装时保留原编码，需要时转换为兼容编码。','Automatic preserves source codecs when possible and uses compatible codecs when needed.')}</p>
    <details><summary>{t('高级选项','Advanced options')}</summary><div className="form-grid"><Field label={t('转换方式','Conversion method')}><select value={strategy} onChange={event=>setStrategy(event.target.value)}><option value="auto">{t('自动：优先直接封装','Automatic: prefer remuxing')}</option><option value="remux">{t('仅换封装（快速）','Remux only (fast)')}</option><option value="transcode">{t('重新编码','Re-encode')}</option></select></Field>{strategy==='transcode'&&<Field label={t('视频编码器','Video encoder')}><select value={encoder} onChange={event=>setEncoder(event.target.value)}>{encoders.filter(value=>value!=='original').map(value=><option key={value} value={value}>{optionLabel(value,t)}</option>)}</select><small className="hint">{t('硬件编码器需要兼容设备','Hardware encoders require compatible devices')}</small></Field>}</div></details>
    {error?<p className="notice danger" role="alert">{explainError(error,t)}</p>:preview?<p className="notice" role="status">{preview.strategy==='remux'?t('将直接换封装，保留原编码。','Will remux and preserve the source codecs.'):t(`将重新编码：${optionLabel(preview.encoder,t)} / ${preview.audioEncoder}。`,`Will re-encode: ${optionLabel(preview.encoder,t)} / ${preview.audioEncoder}.`)}</p>:<p className="hint" role="status">{t('正在检查本地媒体…','Checking local media…')}</p>}
    <p className="hint">{t('使用已保存的本地视频；原始输入保留。','Uses the saved local video and preserves source inputs.')}</p>
  </Modal>;
}
function releaseCheckHint(error: string, t: Translate): string {
  if (/\b(?:403|429)\b|rate limit|too many requests/i.test(error)) return t('GitHub 暂时限制了更新检查请求，请在限额恢复后重试。', 'GitHub has temporarily limited update checks. Retry after the limit resets.');
  if (/timed?\s*out|timeout/i.test(error)) return t('更新检查连接超时，请检查 GitHub 网络连接后重试。', 'The update request timed out. Check GitHub connectivity and retry.');
  return t('暂时无法检查更新，请稍后重试。', 'Cannot check for updates right now. Try again later.');
}
function About({ version, release, busy, error, check, t, run }: { version: string; release: ReleaseStatus | null; busy: boolean; error: string; check: () => Promise<void>; t: Translate; run: Run }) { return <div className="page"><Heading title={t('关于', 'About')} subtitle={t('版本与内置组件。', 'Version and included components.')} /><div className="page-scroll about-content"><section className="panel about-identity"><div className="brand-mark"><Icon name="download" /></div><div><h2>Huifa Media Downloader</h2><p className="muted">{t('版本', 'Version')} {version} · Rust / {host.desktop ? 'Tauri' : 'Web'} / React</p><p>{t('媒体下载、离线处理与封面准备，一个连续的媒体工作空间。', 'A continuous workspace for media downloads, offline processing, and cover preparation.')}</p></div></section><section className="panel settings-group about-update"><h2>{t('版本更新', 'Version updates')}</h2><p>{t('当前版本', 'Current version')} {version}{release && <> · {t('云端版本', 'Latest release')} {release.latestVersion}</>}</p><div className="actions wrap"><Button icon="refresh" disabled={busy} onClick={() => run(check)}>{busy ? t('检查中…', 'Checking…') : t('检查新版本', 'Check for updates')}</Button>{release?.updateAvailable && <Button icon="external" tone="primary" onClick={() => run(() => host.openRelease(release.url))}>{t('打开新版下载页面', 'Open new release page')}</Button>}</div>{error && <p className="hint" role="status"><span>{releaseCheckHint(error, t)}</span><br /><span className="break-text">{error}</span></p>}{release && !release.updateAvailable && !error && <p className="hint">{t('当前已是最新正式版本。', 'You have the latest stable release.')}</p>}</section><section className="panel settings-group"><h2>{t('内置组件', 'Included components')}</h2>{[['yt-dlp', t('跨站媒体信息提取与下载。', 'Cross-site media extraction and downloading.'), 'Unlicense'], ['FFmpeg / FFprobe', t('流合并、媒体探测和转码。', 'Stream merging, media probing, and transcoding.'), t('上游许可证', 'Upstream licenses')], ['QuickJS-NG / yt-dlp-ejs', t('处理来源网站的 JavaScript。', 'JavaScript support for source websites.'), 'MIT / Unlicense'], ['Rust / Tauri / React', t('共享业务核心与桌面、网页界面。', 'Shared business core with desktop and web interfaces.'), 'MIT / Apache-2.0']].map(([name, detail, license]) => <div key={name} className="dependency-row"><strong>{name}</strong><span className="muted">{detail}</span><small>{license}</small></div>)}</section></div></div>; }
