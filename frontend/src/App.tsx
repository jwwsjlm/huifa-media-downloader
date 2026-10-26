import { memo, useCallback, useEffect, useMemo, useRef, useState, type Dispatch } from 'react';
import { host } from './transport';
import { useApp, type Action, type AppState, type ViewState } from './state';
import { localeFor, translate, type Translate } from './i18n';
import { Badge, Button, Check, Empty, Field, Heading, Icon, Modal, Thumbnail } from './components';
import { VirtualList } from './VirtualList';
import { OptionsForm, QualitySelect, automaticQuality, commandOptions, defaults, encoders, optionLabel, qualityLabel } from './options';
import { bytes, completionTime, dimensions, duration, frameRate, mediaQuality, mediaQualityWithFps, stageLabel, stepLabel, statusLabel, statusTone, taskActions, taskProgress, taskSize } from './task';
import { SettingsPage } from './SettingsPage';
import { AppUpdateDialog, AppUpdatePanel, useAppUpdate, type AppUpdateController } from './AppUpdate';
import { McpSettings } from './McpSettings';
import { AddDialog } from './AddDialog';
import { downloadLinks, singleVideoLink } from './links';
import { newRequestId } from './requestId';
import { explainError } from './errors';
import { CoverStudio } from './CoverStudio';
import { TaskContextMenu } from './TaskContextMenu';
import { TaskLogsDialog } from './TaskLogs';
import { DestinationDialog } from './DestinationDialog';
import { CompressionDialog } from './CompressionDialog';
import { useMediaAvailability, type MediaAvailability } from './useMediaAvailability';
import { startStateSync, type StateSyncController } from './stateSync';
import type { AddReport, CollectionEntry, CollectionProbe, CoreEvent, DownloadOptions, Json, Media, MediaInfo, Page, ReleaseStatus, Session, Snapshot, Task, Tool } from './types';

export type Run = (operation: () => Promise<unknown>) => void;
type Dialog = { kind: 'options' } | { kind: 'destination'; task: Task } | { kind: 'requirements'; task: Task } | { kind: 'add'; urls: string[]; options: DownloadOptions; entries: CollectionEntry[]; selectItems: boolean } | { kind: 'delete'; task: Task } | { kind: 'deleteBatch'; tasks: Task[] } | { kind: 'convert'; task: Task } | { kind: 'compress'; task: Task } | { kind: 'logs'; task: Task } | { kind: 'cover'; media: Media };
const taskStatusDetails = ['downloading', 'queued', 'probing', 'waiting_processing', 'processing', 'paused', 'cancelled', 'failed', 'processing_failed'] as const;
export function App() {
  const { state, dispatch } = useApp();
  const [authenticated, setAuthenticated] = useState<boolean | null>(null); const [error, setError] = useState('');
  const nativeUpdates = host.desktop && host.capabilities().updates;
  const appUpdate = useAppUpdate(Boolean(authenticated && state.loaded && nativeUpdates));
  const updateStopped = appUpdate.status?.restartRequired === true;
  const [message, setMessage] = useState(''); const [dialog, setDialog] = useState<Dialog | null>(null);
  const [adding, setAdding] = useState(''); const addingRef = useRef(false);
  const addingOperation = useRef<{ cancelled: boolean; requestId: string } | null>(null);
  const [batchReport, setBatchReport] = useState<AddReport | null>(null);
  const [pendingBatches, setPendingBatches] = useState<AddReport[]>([]);
  const conflictShown = useRef(new Set<string>());
  useEffect(() => {
    for (const id of conflictShown.current) if (!state.tasks[id]?.destinationConflict) conflictShown.current.delete(id);
    if (dialog || batchReport) return;
    const task = state.taskIds.map(id => state.tasks[id]).find(task => task.destinationConflict && ['failed','processing_failed'].includes(task.status) && !conflictShown.current.has(task.id));
    if (task) { conflictShown.current.add(task.id); setDialog({ kind: 'destination', task }); }
  }, [state.taskChangeVersion, dialog, batchReport]);
  const cancelAdding = () => { const operation = addingOperation.current; if (!operation) return; operation.cancelled = true; void host.command('probe.cancel', { id: operation.requestId }).catch(() => {}); setAdding(translateRef.current('正在取消…', 'Cancelling…')); };
  const [release, setRelease] = useState<ReleaseStatus | null>(null); const [releaseBusy, setReleaseBusy] = useState(false); const [releaseError, setReleaseError] = useState('');
  const releaseChecked = useRef(false);
  const checkRelease = useCallback(async () => { setReleaseBusy(true); setReleaseError(''); try { setRelease(await host.releaseStatus()); } catch (reason) { setReleaseError(String(reason)); } finally { setReleaseBusy(false); } }, []);
  useEffect(() => { if (authenticated && state.loaded && !nativeUpdates && !releaseChecked.current) { releaseChecked.current = true; void checkRelease(); } }, [authenticated, state.loaded, nativeUpdates, checkRelease]);
  const locale = localeFor(state.settings); const t = useCallback<Translate>((zh, en) => translate(locale, zh, en), [locale]);
  useEffect(() => {
    const finished = pendingBatches.filter(batch => batch.ids.length > 0 && batch.ids.every(id => state.tasks[id] && ['completed', 'retained', 'failed', 'processing_failed', 'cancelled'].includes(state.tasks[id].status)));
    if (!finished.length) return;
    const issues = finished.flatMap(batch => batch.ids.map(id => state.tasks[id]).filter(task => ['failed', 'processing_failed', 'cancelled'].includes(task.status)).map(task => ({ url: task.url, error: task.error || statusLabel(task.status, t) })));
    setPendingBatches(current => current.filter(batch => !finished.includes(batch)));
    if (issues.length) setBatchReport(current => { const results = [...new Map([...(current?.results || []), ...finished.flatMap(batch => batch.results)].map(result => [result.id, result])).values()]; return { ids: results.map(result => result.id), results, issues: [...(current?.issues || []), ...issues], cancelled: current?.cancelled }; });
    else setMessage(t(`批次下载已完成：${finished.reduce((count, batch) => count + batch.ids.length, 0)} 项。`, `Batch downloads completed: ${finished.reduce((count, batch) => count + batch.ids.length, 0)} tasks.`));
  }, [pendingBatches, state.taskChangeVersion, t]);
  const settingsRef = useRef(state.settings); settingsRef.current = state.settings;
  const stateRef = useRef(state); stateRef.current = state;
  const syncRef = useRef<StateSyncController | null>(null);
  const refreshState = useCallback(() => syncRef.current?.refresh() ?? Promise.resolve(), []);
  const translateRef = useRef(t); translateRef.current = t;
  const run = useCallback<Run>(operation => { setError(''); void operation().catch(reason => setError(reason instanceof Error ? reason.message : String(reason))); }, []);
  const navigate = useCallback((page: Page) => {
    if (!host.desktop) history.pushState(null, '', page === 'mcp' ? '/mcp' : '/');
    dispatch({ type: 'view', patch: { page } });
  }, [dispatch]);
  useEffect(() => { const changed = () => dispatch({ type: 'view', patch: { page: /^\/mcp\/?$/.test(location.pathname) ? 'mcp' : 'downloads' } }); window.addEventListener('popstate', changed); return () => window.removeEventListener('popstate', changed); }, [dispatch]);
  useEffect(() => { const expired = () => setAuthenticated(false); window.addEventListener('huifa-session-expired', expired); return () => window.removeEventListener('huifa-session-expired', expired); }, []);
  useEffect(() => { let active = true; void host.session().then(session => { if (active) setAuthenticated(session.authenticated); }).catch(reason => { if (active) { setError(String(reason)); setAuthenticated(false); } }); return () => { active = false; }; }, []);
  useEffect(() => {
    if (!authenticated) return;
    let toolsChecked = false; const notified = new Set<string>(); const bufferedCompletions = new Set<string>();
    const observe = (event: CoreEvent) => {
      if (event.kind === 'task.remove') { const id = typeof event.data === 'string' ? event.data : String((event.data as { id: string }).id); notified.delete(id); bufferedCompletions.delete(id); }
      if (event.kind !== 'task.upsert') return;
      const task = event.data as unknown as Task;
      if (task.status === 'completed' && !notified.has(task.id)) { notified.add(task.id); if (settingsRef.current.desktopNotifications === true) run(() => host.notify(translateRef.current('下载完成', 'Download complete'), task.title)); }
    };
    const sync = startStateSync({
      host, dispatch, getTask: id => stateRef.current.tasks[id], shouldPoll: () => stateRef.current.pendingTaskCount > 0,
      onError: reason => setError(String(reason)),
      onSnapshot: (snapshot, signal) => {
        const ids = new Set(snapshot.tasks.map(task => task.id));
        for (const id of notified) if (!ids.has(id) && !bufferedCompletions.has(id)) notified.delete(id);
        bufferedCompletions.clear();
        if (toolsChecked) return;
        toolsChecked = true;
        for (const task of snapshot.tasks) if (task.status === 'completed') notified.add(task.id);
        void host.command<Tool[]>('tools.check', {}, { signal, timeoutMs: 15000 }).then(tools => { if (!signal.aborted) dispatch({ type: 'event', event: { kind: 'tools.updated', data: tools as unknown as Json } }); }).catch(() => { if (!signal.aborted) setMessage(translateRef.current('未能检查运行时组件，可在“工具与更新”重试。', 'Runtime components could not be checked. Retry under Tools & updates.')); });
      },
      onEvent: observe, onBufferedEvent: event => {
        if (event.kind !== 'task.upsert' || (event.data as unknown as Task).status !== 'completed') return;
        bufferedCompletions.add((event.data as unknown as Task).id); observe(event);
      },
    });
    syncRef.current = sync;
    return () => { sync.dispose(); if (syncRef.current === sync) syncRef.current = null; };
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
      if (dialog || updateStopped || !authenticated || !event.ctrlKey && !event.metaKey) return;
      if (/^[1-4]$/.test(event.key)) { event.preventDefault(); dispatch({ type: 'view', patch: { page: (['downloads', 'completed', 'settings', 'about'] as Page[])[Number(event.key) - 1] } }); }
      if (event.key.toLowerCase() === 'f') { const id = state.view.page === 'downloads' ? 'task-search' : state.view.page === 'completed' ? 'media-search' : ''; if (id) { event.preventDefault(); document.getElementById(id)?.focus(); } }
    };
    document.addEventListener('keydown', handle); return () => document.removeEventListener('keydown', handle);
  }, [dialog, updateStopped, authenticated, state.view.page, dispatch]);
  useEffect(() => { if (!message) return; const timer = setTimeout(() => setMessage(''), 5500); return () => clearTimeout(timer); }, [message]);
  const quality = state.view.quality;
  const options = useMemo(() => { const saved = { ...defaults, ...state.settings.downloadOptions }; return quality === null ? saved : { ...saved, ...automaticQuality(saved, quality) }; }, [state.settings.downloadOptions, quality]);
  const setQuality = useCallback((quality: string | null) => dispatch({ type: 'view', patch: { quality } }), [dispatch]);
  const add = useCallback((urls: string[], clipboard = false) => {
    if (addingRef.current) return false;
    const operation = { cancelled: false, requestId: newRequestId() };
    addingRef.current = true;
    addingOperation.current = operation; setBatchReport(null);
    setAdding(clipboard ? t('读取剪贴板…', 'Reading clipboard…') : t('识别链接…', 'Checking links…'));
    run(async () => {
      try {
        let text = urls.join('\n');
        if (clipboard) { try { text = await host.readClipboard(); } catch { throw new Error(t('无法读取剪贴板，请手动粘贴链接。', 'Cannot read the clipboard. Paste the links manually.')); } }
        if (operation.cancelled) return;
        const links = downloadLinks(text); const entries: CollectionEntry[] = []; const issues: NonNullable<AddReport['issues']> = []; let collection = false;
        for (const [index, url] of links.entries()) {
          if (operation.cancelled) break;
          setAdding(t(`识别链接 ${index + 1}/${links.length}…`, `Checking links ${index + 1}/${links.length}…`));
          if (singleVideoLink(url) || options.collectionMode === 'single') { entries.push({ url, title: url }); continue; }
          try {
            const result = await host.command<CollectionProbe>('probe.collection', { url, options: commandOptions(options), requestId: operation.requestId });
            if (operation.cancelled) break;
            collection ||= result.kind === 'collection' || !result.kind && result.entries?.length > 1;
            entries.push(...(result.entries || [{ url, title: result.title || url }]));
          } catch (reason) { if (!operation.cancelled) issues.push({ url, error: String(reason) }); }
        }
        if (operation.cancelled) { setBatchReport({ ids: [], results: [], issues, cancelled: true }); return; }
        const distinct = [...new Map(entries.map(entry => [entry.url, entry])).values()];
        if (!distinct.length) { setBatchReport({ ids: [], results: [], issues }); return; }
        if (collection && options.collectionMode !== 'all' || options.contentMode === 'manual') { setDialog({ kind: 'add', urls: distinct.map(entry => entry.url), entries: [...distinct, ...issues.map(issue => ({ ...issue, title: issue.url, available: false }))], options, selectItems: collection && options.collectionMode !== 'all' }); return; }
        setAdding(t('加入队列中…', 'Adding to queue…'));
        const results: AddReport['results'] = []; const ids: string[] = [];
        for (const entry of distinct) {
          if (operation.cancelled) break;
          try { const report = await host.command<AddReport>('tasks.add', { urls: [entry.url], options: commandOptions({ ...options, collectionMode: 'single' }), report: true }); if (report?.results) { results.push(...report.results); ids.push(...report.ids); } }
          catch (reason) { issues.push({ url: entry.url, error: String(reason) }); }
        }
        const existing = results.find(result => result.action === 'existing');
        dispatch({ type: 'view', patch: { urls: issues.map(issue => issue.url).join('\n'), ...(existing ? { taskSelection: existing.id } : {}) } });
        if (issues.length || operation.cancelled) setBatchReport({ ids, results, issues, cancelled: operation.cancelled });
        if (ids.length) setPendingBatches(current => [...current, { ids: [...new Set(ids)], results }]);
        setMessage(existing ? existing.status === 'paused' ? t('该任务已暂停，可在任务详情中继续。', 'This task is paused. Resume it from task details.') : t(`该任务已存在：${statusLabel(existing.status,t)}，不会重复下载。`, `This task already exists: ${statusLabel(existing.status,t)}. No duplicate was added.`) : t('任务已加入下载队列', 'Tasks added to the download queue'));
      } finally { if (addingOperation.current === operation) { addingRef.current = false; setAdding(''); addingOperation.current = null; } }
    });
    return true;
  }, [options, run, t, dispatch]);
  const clipboardSeen = useRef(new Set<string>()); const clipboardReading = useRef(false);
  const clipboardImport = useRef({ add, blocked: false }); clipboardImport.current = { add, blocked: Boolean(dialog || batchReport || updateStopped) };
  useEffect(() => {
    if (!host.desktop || !authenticated || !state.loaded || state.settings.clipboardMonitoring !== true) return;
    let disposed = false; let warned = false;
    const poll = async () => {
      if (disposed || clipboardReading.current || addingRef.current || clipboardImport.current.blocked) return;
      clipboardReading.current = true;
      try {
        const text = await host.readClipboard();
        if (disposed || addingRef.current || clipboardImport.current.blocked) return;
        let links: string[];
        try { links = downloadLinks(text); } catch { return; }
        const fresh = links.filter(url => !clipboardSeen.current.has(url));
        if (fresh.length && clipboardImport.current.add(fresh)) for (const url of fresh) clipboardSeen.current.add(url);
      } catch {
        if (!disposed && !warned) { warned = true; setMessage(translateRef.current('剪贴板监控暂时无法读取文本，请检查剪贴板访问权限；应用会继续尝试。', 'Clipboard monitoring could not read text. Check clipboard access; the app will keep trying.')); }
      } finally { clipboardReading.current = false; }
    };
    void poll(); const timer = window.setInterval(() => { void poll(); }, 1000);
    return () => { disposed = true; window.clearInterval(timer); };
  }, [authenticated, state.loaded, state.settings.clipboardMonitoring]);
  const selectTask = useCallback((id: string) => dispatch({ type: 'view', patch: { taskSelection: id } }), [dispatch]);
  const selectMedia = useCallback((id: string) => dispatch({ type: 'view', patch: { mediaSelection: id } }), [dispatch]);
  const taskAction = useCallback((task: Task, action: string) => {
    if (action === 'delete') { setDialog({ kind: 'delete', task }); return; }
    if (action === 'convert') { setDialog({ kind: 'convert', task }); return; }
    if (action === 'compress') { setDialog({ kind: 'compress', task }); return; }
    if (action === 'logs') { setDialog({ kind: 'logs', task }); return; }
    if (action === 'configure') { setDialog({ kind: 'requirements', task }); return; }
    if (action === 'destination') { setDialog({ kind: 'destination', task }); return; }
    run(async () => {
      const updated = await host.command<Task>(`tasks.${action}`, { id: task.id });
      if (['retry', 'resume', 'retryCurrent', 'compressRetryCpu'].includes(action)) {
        dispatch({ type: 'event', event: { kind: 'task.upsert', data: updated as unknown as Json }, ifTaskUnchanged: task });
        setMessage(t('请求已受理，任务将按队列执行。', 'Request accepted. The task will follow the queue.'));
      }
    });
  }, [run, dispatch, t]);
  const cover = useCallback((media: Media) => setDialog({ kind: 'cover', media }), []);
  // Progress, speed and ETA do not change list membership, order or status counts.
  const taskIds = useMemo(() => {
    const { view, tasks } = state; const search = view.taskSearch.toLowerCase();
    return state.taskIds.filter(id => { const task = tasks[id]; return (!search || `${task.title} ${task.url} ${id}`.toLowerCase().includes(search)) && (view.taskStatus === 'all' || view.taskStatus === 'active' ? view.taskStatus !== 'active' || ['queued', 'probing', 'downloading', 'waiting_processing', 'processing'].includes(task.status) : view.taskStatus === 'completed' ? ['completed', 'retained'].includes(task.status) : task.status === view.taskStatus); }).sort((a, b) => view.taskSort === 'title' ? tasks[a].title.localeCompare(tasks[b].title) : view.taskSort === 'oldest' ? tasks[a].createdAt.localeCompare(tasks[b].createdAt) : tasks[b].createdAt.localeCompare(tasks[a].createdAt));
  }, [state.taskIds, state.taskListVersion, state.view.taskSearch, state.view.taskStatus, state.view.taskSort]);
  const taskSummary = useMemo(() => {
    const ids = state.view.page === 'downloads' ? taskIds : state.taskIds;
    const counts = Object.fromEntries(taskStatusDetails.map(status => [status, 0])); let completed = 0; let other = 0;
    for (const id of ids) {
      const status = state.tasks[id].status;
      if (status === 'completed' || status === 'retained') completed += 1;
      else if (Object.hasOwn(counts, status)) counts[status] += 1;
      else other += 1;
    }
    return { total: ids.length, completed, unfinished: ids.length - completed, counts, other };
  }, [taskIds, state.taskIds, state.taskListVersion, state.view.page]);
  if (authenticated === null) return <div className="web-login"><Empty title={t('连接下载服务', 'Connecting to download service')} /></div>;
  if (!authenticated && host.desktop) return <div className="web-login"><div className="panel web-login-form"><h2>{t('下载服务未能启动', 'Download service could not start')}</h2><p className="notice danger" role="alert">{explainError(error, t)}</p><Button tone="primary" onClick={() => run(async () => { await host.session(); setError(''); setAuthenticated(true); })}>{t('重试连接', 'Retry connection')}</Button></div></div>;
  if (!authenticated) return <Login t={t} error={error} run={run} connected={() => { setError(''); setAuthenticated(true); }} />;
  return <>
    <div className="app-shell" inert={updateStopped || undefined}><nav className="navigation" aria-label={t('导航', 'Navigation')}>
      <div className="brand"><div className="brand-mark"><Icon name="download" /></div><div><strong>Huifa</strong><small>MEDIA WORKSPACE</small></div></div>
      <div className="nav-links">{([['downloads', '下载任务', 'Downloads', 'download'], ['completed', '已完成', 'Completed', 'library'], ['mcp', 'AI 媒体服务（MCP）', 'AI media service (MCP)', 'video'], ['settings', '设置', 'Settings', 'settings'], ['about', '关于', 'About', 'info']] as const).map(([page, zh, en, icon]) => <button key={page} className={`nav-link ${state.view.page === page ? 'active' : ''}`} aria-current={state.view.page === page ? 'page' : undefined} title={t(zh, en)} aria-label={t(zh, en)} onClick={() => navigate(page)}><Icon name={icon} /><span>{page === 'mcp' ? t('AI 媒体服务', 'AI media service') : t(zh, en)}</span></button>)}</div>
      {state.view.page === 'downloads' && <div className="nav-categories"><small className="category-label">{t('任务分类', 'Task categories')}</small>{['all', 'active', 'queued', 'waiting_processing', 'processing', 'paused', 'completed', 'processing_failed', 'failed'].map(status => <button key={status} className={`nav-link category-link ${state.view.taskStatus === status ? 'active' : ''}`} aria-pressed={state.view.taskStatus === status} title={status === 'all' ? t('全部任务', 'All tasks') : status === 'active' ? t('进行中', 'Active') : statusLabel(status, t)} onClick={() => dispatch({ type: 'view', patch: { taskStatus: status } })}><Icon name={status === 'all' ? 'download' : status === 'completed' ? 'check' : status === 'paused' ? 'pause' : status.includes('failed') ? 'info' : 'play'} /><span>{status === 'all' ? t('全部任务', 'All tasks') : status === 'active' ? t('进行中', 'Active') : statusLabel(status, t)}</span></button>)}</div>}
      <div className="nav-bottom"><span className={`connection-dot ${state.connected ? '' : 'offline'}`} /><span>{state.connected ? t('服务已连接', 'Service connected') : t('连接恢复中', 'Reconnecting')}</span><button className="version-link" title={nativeUpdates || !release?.updateAvailable ? t('版本与更新', 'Version and updates') : t('打开新版下载页面', 'Open new release page')} onClick={() => !nativeUpdates && release?.updateAvailable ? run(() => host.openRelease(release.url)) : navigate('about')}>v{state.version}{nativeUpdates ? appUpdate.status?.version && <span className="badge">{appUpdate.status.canInstall ? t('待安装', 'Ready to install') : ['downloading', 'verifying'].includes(appUpdate.status.phase) ? t('更新中', 'Updating') : t('有新版', 'Update')}</span> : release?.updateAvailable && <span className="badge">{t('有新版', 'Update')}</span>}</button></div>
    </nav><main className="main" id="main-content">
      {!state.loaded ? <Empty title={t('载入任务、媒体和设置…', 'Loading tasks, media, and settings…')} /> : state.view.page === 'downloads' ? <Downloads ids={taskIds} adding={adding} cancelAdding={cancelAdding} clipboard={() => add([], true)} state={state} dispatch={dispatch} options={options} changeQuality={setQuality} add={add} select={selectTask} action={taskAction} bulkDelete={ids => setDialog({ kind: 'deleteBatch', tasks: ids.map(id => state.tasks[id]).filter(Boolean) })} run={run} quick={() => setDialog({ kind: 'options' })} t={t} /> : state.view.page === 'completed' ? <Completed state={state} dispatch={dispatch} select={selectMedia} action={taskAction} cover={cover} run={run} refresh={refreshState} t={t} /> : state.view.page === 'mcp' ? <div className="page mcp-workspace page-scroll"><McpSettings run={run} t={t} /></div> : state.view.page === 'settings' ? <SettingsPage state={state} dispatch={dispatch} t={t} run={run} message={setMessage} appUpdate={nativeUpdates ? appUpdate : undefined} /> : <About version={state.version} release={release} busy={releaseBusy} error={releaseError} check={checkRelease} t={t} run={run} appUpdate={nativeUpdates ? appUpdate : undefined} />}
    </main></div>
    <footer className="statusbar" inert={updateStopped || undefined}>
      <div className="status-overview"><div className="status-totals" role="group" aria-live="polite" aria-atomic="true" aria-label={t('任务统计', 'Task statistics')}>
        <span className="status-scope">{state.view.page === 'downloads' ? t(`当前列表 ${taskSummary.total} 项`, `Current list: ${taskSummary.total}`) : t(`全部任务 ${taskSummary.total} 项`, `All tasks: ${taskSummary.total}`)}</span>
        {state.view.page === 'downloads' && (state.view.taskSearch || state.view.taskStatus !== 'all') && <span>{t(`全部 ${state.taskIds.length} 项`, `${state.taskIds.length} total`)}</span>}
        <span className={`status-count ${taskSummary.completed ? 'success' : ''}`}>{t(`已完成 ${taskSummary.completed}`, `Completed ${taskSummary.completed}`)}</span>
        <span className="status-count" title={t('包含排队、运行、暂停、失败和取消的任务', 'Includes queued, running, paused, failed and cancelled tasks')}>{t(`未完成 ${taskSummary.unfinished}`, `Unfinished ${taskSummary.unfinished}`)}</span>
      </div><span className="status-speed" aria-label={t('总下载速度', 'Total download speed')}>{t('总下载速度', 'Total download speed')}：{state.connected ? state.downloadSpeed > 0 ? `${bytes(state.downloadSpeed)}/s` : '0 B/s' : '—'}</span><span className="status-message">{message || (!state.connected ? t('连接恢复后自动同步任务。', 'Tasks sync when the connection returns.') : t('就绪', 'Ready'))}</span><span className="status-shortcut">{t('Ctrl+1–4 切换页面', 'Ctrl+1–4 switch pages')}</span>{!host.desktop && <Button tone="text" onClick={() => run(async () => { await host.logout(); setAuthenticated(false); })}>{t('退出登录', 'Sign out')}</Button>}</div>
      <div className="status-breakdown" role="group" aria-label={t('任务状态明细', 'Task status breakdown')}>{taskStatusDetails.map(status => <button type="button" key={status} className={`status-count status-filter ${taskSummary.counts[status] ? statusTone(status) : ''}`} aria-pressed={state.view.page === 'downloads' && state.view.taskStatus === status} title={t(`查看${statusLabel(status, t)}的任务`, `Show ${statusLabel(status, t).toLowerCase()} tasks`)} onClick={() => { navigate('downloads'); dispatch({ type: 'view', patch: { taskStatus: status } }); }}>{`${statusLabel(status, t)} ${taskSummary.counts[status]}`}</button>)}{taskSummary.other > 0 && <span className="status-count">{t(`其他 ${taskSummary.other}`, `Other ${taskSummary.other}`)}</span>}</div>
      {(error || message) && <div className="toasts" aria-live="polite"><div className={`toast ${error ? 'danger' : ''}`}><span role={error ? 'alert' : 'status'}>{error ? explainError(error, t) : message}</span><Button icon="close" tone="icon-only" aria-label={t('关闭提示', 'Dismiss message')} onClick={() => { setError(''); setMessage(''); }} /></div></div>}
    </footer>
    {dialog?.kind === 'add' && <AddDialog urls={dialog.urls} options={dialog.options} initialEntries={dialog.entries} selectItems={dialog.selectItems} t={t} run={run} close={() => setDialog(null)} added={report => { const existing = report?.results.find(result => result.action === 'existing'); dispatch({ type: 'view', patch: { urls: report?.issues?.map(issue => issue.url).join('\n') || '', ...(existing ? { taskSelection: existing.id } : {}) } }); if (report?.issues?.length || report?.cancelled) setBatchReport(report); if (report?.ids.length) setPendingBatches(current => [...current, { ids: [...new Set(report.ids)], results: report.results }]); setDialog(null); setMessage(existing ? existing.status === 'paused' ? t('该任务已暂停，可在任务详情中继续。', 'This task is paused. Resume it from task details.') : t(`该任务已存在：${statusLabel(existing.status,t)}，不会重复下载。`, `This task already exists: ${statusLabel(existing.status,t)}. No duplicate was added.`) : t('任务已加入下载队列', 'Tasks added to the download queue')); }} />}
    {dialog?.kind === 'requirements' && <RequirementsDialog task={dialog.task} t={t} close={() => setDialog(null)} restarted={() => { setDialog(null); setMessage(t('已按调整后的要求重新加入任务', 'Task restarted with the adjusted requirements')); }} />}
    {dialog?.kind === 'destination' && <DestinationDialog task={state.tasks[dialog.task.id] || dialog.task} t={t} close={() => setDialog(null)} />}
    {batchReport && !dialog && <Modal title={t('批量结果', 'Batch results')} t={t} close={() => setBatchReport(null)} footer={<Button onClick={() => setBatchReport(null)}>{t('关闭', 'Close')}</Button>}><p>{t(`已加入或定位 ${batchReport.results.length} 项任务，${batchReport.issues?.length || 0} 项异常或待处理。`, `${batchReport.results.length} tasks added or located, ${batchReport.issues?.length || 0} issues or decisions needed.`)}</p>{batchReport.cancelled && <p className="notice">{t('已取消剩余识别和添加，已创建的任务继续执行。', 'Remaining checks and additions were cancelled. Created tasks keep running.')}</p>}{batchReport.issues?.map((issue, index) => <p className="notice danger break-text" key={`${issue.url}-${index}`}><strong>{issue.url}</strong><br />{explainError(issue.error, t)}</p>)}</Modal>}
    {dialog?.kind === 'options' && <QuickOptions initial={options} t={t} close={() => setDialog(null)} apply={async next => { const settings = await host.command<Snapshot['settings']>('settings.save', { settings: { ...state.settings, downloadOptions: commandOptions(next) } }); dispatch({ type: 'event', event: { kind: 'settings.updated', data: settings } }); setQuality(null); setDialog(null); }} />}
    {(dialog?.kind === 'delete' || dialog?.kind === 'deleteBatch') && <DeleteDialog tasks={dialog.kind === 'delete' ? [dialog.task] : dialog.tasks} batch={dialog.kind === 'deleteBatch'} initialFiles={state.settings.uiPreferences?.deleteMediaOnTaskDelete === true} remember={async files => { const settings = await host.command<Snapshot['settings']>('settings.preferences', { preferences: { deleteMediaOnTaskDelete: files } }); dispatch({ type: 'event', event: { kind: 'settings.updated', data: settings } }); }} t={t} run={run} refresh={refreshState} close={() => setDialog(null)} />}
    {dialog?.kind === 'convert' && <ConvertDialog task={dialog.task} t={t} run={run} close={() => setDialog(null)} />}
    {dialog?.kind === 'compress' && <CompressionDialog task={dialog.task} t={t} run={run} close={() => setDialog(null)} queued={task => { dispatch({ type: 'event', event: { kind: 'task.upsert', data: task as unknown as Json }, onlyIfTaskAbsent: true }); dispatch({ type: 'view', patch: { page: 'downloads', taskSelection: task.id, taskStatus: 'all', taskSearch: '' } }); setMessage(t('压缩任务已加入处理队列', 'Compression added to the processing queue')); }} />}
    {dialog?.kind === 'cover' && <CoverStudio media={dialog.media} t={t} run={run} close={() => setDialog(null)} />}
    {dialog?.kind === 'logs' && <TaskLogsDialog task={state.tasks[dialog.task.id] || dialog.task} t={t} close={() => setDialog(null)} />}
    {nativeUpdates && <AppUpdateDialog update={appUpdate} t={t} />}
  </>;
}
function Login({ t, error, connected, run }: { t: Translate; error: string; connected: () => void; run: Run }) {
  const [account, setAccount] = useState(''); const [password, setPassword] = useState(''); const [busy, setBusy] = useState(false);
  return <div className="web-login"><form className="panel web-login-form" onSubmit={event => { event.preventDefault(); setBusy(true); run(async () => { try { await host.login(account, password); setPassword(''); connected(); } finally { setBusy(false); } }); }}>
    <div className="brand"><div className="brand-mark"><Icon name="download" /></div><div><strong>Huifa</strong><small>MEDIA DOWNLOADER</small></div></div><h2>{t('登录下载工作空间', 'Sign in to your download workspace')}</h2>
    <Field label={t('账号', 'Account')}><input autoFocus value={account} onChange={event => setAccount(event.target.value)} autoComplete="username" required maxLength={128} /></Field>
    <Field label={t('密码', 'Password')}><input type="password" value={password} onChange={event => setPassword(event.target.value)} autoComplete="current-password" required maxLength={1024} /></Field>
    {error && <p className="notice danger" role="alert">{explainError(error, t)}</p>}<Button type="submit" tone="primary" disabled={busy}>{busy ? t('连接中…', 'Connecting…') : t('登录', 'Sign in')}</Button>
  </form></div>;
}
const Composer = memo(function Composer({ urls, options, change, changeQuality, add, quick, t, adding, cancelAdding, clipboard }: { urls: string; options: DownloadOptions; change: (value: string) => void; changeQuality: (quality: string) => void; add: (urls: string[]) => void; quick: () => void; t: Translate; adding: string; cancelAdding: () => void; clipboard: () => void }) {
  return <form className="composer panel" onSubmit={event => { event.preventDefault(); const links = urls.split(/\s+/).map(value => value.trim()).filter(Boolean); if (links.length) add([...new Set(links)]); }}>
    <div className="composer-title"><strong>{t('新建下载', 'New download')}</strong><span className="muted">{t('支持单条、合集和批量链接', 'Single links, collections, and batches')}</span>
      <div className="batch-plan" title={t('加入后方案固定；设置更改仅影响新任务', 'The plan is fixed when added; settings affect new tasks only')}><Badge>{t('此批次计划', 'Batch plan')}</Badge><label className="quality-picker">{t('画质', 'Quality')}<QualitySelect options={options} change={changeQuality} t={t} /></label><span className="plan-summary">{options.contentMode === 'audio' ? t('仅音频', 'Audio only') : options.contentMode === 'manual' ? t('手动选格式', 'Choose format') : t('视频与音频', 'Video with audio')} · {optionLabel(String(options.encoder || 'copy'), t)} · {optionLabel(String(options.subtitleLanguage || 'none'), t)}</span></div>
      <Button icon="settings" size="small" tone="subtle" onClick={quick}>{t('快速选项', 'Quick options')}</Button></div>
    <div className="composer-input"><textarea aria-label={t('下载链接', 'Download links')} rows={2} value={urls} placeholder={t('粘贴视频或合集链接，每行一个…', 'Paste video or collection links, one per line…')} onChange={event => change(event.target.value)} onKeyDown={event => { if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') { event.preventDefault(); event.currentTarget.form?.requestSubmit(); } }} /><Button icon="copy" disabled={Boolean(adding)} onClick={clipboard}>{t('剪贴板下载', 'Download clipboard')}</Button><Button icon="plus" type="submit" tone="primary" disabled={Boolean(adding) || !urls.trim()}>{adding || t('添加下载', 'Add downloads')}</Button>{adding && <Button onClick={cancelAdding}>{t('取消识别', 'Cancel checks')}</Button>}</div>
  </form>;
});
function Progress({ task }: { task: Task }) { const progress = taskProgress(task); return <div className={`progress ${progress.indeterminate ? 'indeterminate' : ''}`} role="progressbar" aria-label={task.stage || task.status} aria-valuemin={0} aria-valuemax={100} aria-valuenow={progress.indeterminate ? undefined : progress.value}><span style={{ width: `${progress.value}%` }} /></div>; }
function availabilityLabel(availability: MediaAvailability | undefined, t: Translate) { return availability === 'missing' ? t('不存在源文件', 'Source file missing') : availability === 'unknown' ? t('暂时无法访问', 'File temporarily inaccessible') : ''; }
function FileAvailability({ availability, t }: { availability?: MediaAvailability; t: Translate }) { const label = availabilityLabel(availability, t); return label ? <p className="notice" role="status">{label}</p> : null; }
const TaskRow = memo(function TaskRow({ task, mediaId, availability, timeFormat, t }: { task: Task; mediaId?: string; availability?: MediaAvailability; timeFormat?: string; t: Translate }) {
  const progress = taskProgress(task);
  const quality = task.status === 'completed' ? mediaQualityWithFps(task.mediaInfo) : '—';
  const size = taskSize(task, t);
  const completed = ['completed', 'retained'].includes(task.status) ? completionTime(task.completedAt, timeFormat) : '';
  return <div className="task-row-content task-columns" title={task.title || task.url}><div className="task-name"><Thumbnail url={task.hasThumbnail ? host.assetUrl(task.id, 'taskThumbnail') : mediaId ? host.assetUrl(mediaId, 'thumbnail') : undefined} /><div className="row-main"><strong className="truncate">{task.title || t('等待获取媒体信息', 'Waiting for media information')}</strong><small className={`task-status truncate ${statusTone(task.status)}`}>{statusLabel(task.status, t)}{availabilityLabel(availability, t) && <> · {availabilityLabel(availability, t)}</>}{completed && <time className="task-completed-time" dateTime={task.completedAt!} title={completionTime(task.completedAt)} aria-label={t('完成时间', 'Completed at')}> · {completed}</time>}</small></div></div><div className="task-stage"><div className="task-stage-label"><span className="truncate" title={quality === '—' ? undefined : `${quality} · ${dimensions(task.mediaInfo)}`}>{stepLabel(task)}{stageLabel(task.stage, task.status, t)}{quality !== '—' && <> · {quality}</>}</span><small>{progress.indeterminate ? '—' : `${progress.value.toFixed(1)}%`}</small></div><Progress task={task} /></div><span className="task-speed truncate">{typeof task.speed === 'number' ? `${bytes(task.speed)}/s` : task.speed || '—'}</span><span className="task-eta truncate">{duration(task.eta)}</span><span className="task-size" title={size.title}><span>{size.value}</span>{size.detail && <small>{size.detail}</small>}</span></div>;
});
function Downloads({ ids, state, dispatch, options, changeQuality, add, select, action, bulkDelete, quick, run, t, adding, cancelAdding, clipboard }: { ids: string[]; state: AppState; dispatch: Dispatch<Action>; options: DownloadOptions; changeQuality: (quality: string) => void; add: (urls: string[]) => void; select: (id: string) => void; action: (task: Task, action: string) => void; bulkDelete: (ids: string[]) => void; quick: () => void; run: Run; t: Translate; adding: string; cancelAdding: () => void; clipboard: () => void }) {
  const { view, tasks } = state;
  const [context, setContext] = useState<{ id: string; position: { x: number; y: number }; trigger: HTMLElement } | null>(null);
  const closeContext = useCallback(() => setContext(null), []);
  const openContext = useCallback((id: string, position: { x: number; y: number }, trigger: HTMLElement) => setContext({ id, position, trigger }), []);
  const selected = view.taskSelection;
  const [visibleIds, setVisibleIds] = useState<string[]>([]);
  const availabilityIds = useMemo(() => [...new Set([...visibleIds, selected].map(id => state.mediaByTask[id]).filter(Boolean))], [visibleIds, selected, state.mediaByTask]);
  const { availability } = useMediaAvailability(availabilityIds);
  const [savedSelection, setSelection] = useState<string[]>([]);
  const primary = useRef(selected);
  const selection = primary.current === selected ? savedSelection : ids.includes(selected) ? [selected] : [];
  const selectInList = useCallback((id: string) => { primary.current = id; select(id); }, [select]);
  useEffect(() => { if (primary.current !== selected) { primary.current = selected; setSelection(ids.includes(selected) ? [selected] : []); } }, [selected, ids]);
  useEffect(() => { const visible = new Set(ids); setSelection(current => { const next = current.filter(id => visible.has(id)); return next.length === current.length ? current : next; }); }, [ids]);
  useEffect(() => { if (context && !ids.includes(context.id)) closeContext(); }, [context, ids, closeContext]);
  const hidden = useMemo(() => !ids.includes(selected), [ids, selected]);
  const timeFormat = typeof state.settings.completedTimeFormat === 'string' ? state.settings.completedTimeFormat : undefined;
  const row = useCallback((id: string) => <TaskRow task={tasks[id]} mediaId={state.mediaByTask[id]} availability={availability[state.mediaByTask[id]]} timeFormat={timeFormat} t={t} />, [tasks, state.mediaByTask, availability, timeFormat, t]);
  const urlsChange = useCallback((urls: string) => dispatch({ type: 'view', patch: { urls } }), [dispatch]);
  return <div className="page downloads-page"><Heading title={t('下载任务', 'Downloads')} subtitle={t('下载与处理独立排队，原始媒体先保存到本地。', 'Downloads and processing have separate queues. Source media is saved first.')}>{host.desktop && <Button icon="folder" onClick={() => run(() => host.openDownloadDirectory())}>{t('下载目录', 'Download folder')}</Button>}</Heading>
    <Composer adding={adding} cancelAdding={cancelAdding} clipboard={clipboard} urls={view.urls} options={options} change={urlsChange} changeQuality={changeQuality} add={add} quick={quick} t={t} />
    <div className="toolbar"><input id="task-search" aria-label={t('搜索任务', 'Search tasks')} type="search" value={view.taskSearch} placeholder={t('搜索所有任务：标题、链接、编号', 'Search all tasks: title, URL, ID')} onChange={event => dispatch({ type: 'view', patch: { taskSearch: event.target.value } })} /><select aria-label={t('状态筛选', 'Status filter')} value={view.taskStatus} onChange={event => dispatch({ type: 'view', patch: { taskStatus: event.target.value } })}>{['all', 'active', 'queued', 'probing', 'downloading', 'waiting_processing', 'processing', 'paused', 'completed', 'processing_failed', 'failed', 'cancelled'].map(status => <option key={status} value={status}>{status === 'all' ? t('全部状态', 'All statuses') : status === 'active' ? t('进行中', 'Active') : statusLabel(status, t)}</option>)}</select><select aria-label={t('排序', 'Sort')} value={view.taskSort} onChange={event => dispatch({ type: 'view', patch: { taskSort: event.target.value } })}><option value="newest">{t('最新优先', 'Newest first')}</option><option value="oldest">{t('最早优先', 'Oldest first')}</option><option value="title">{t('标题排序', 'Title')}</option></select><Button icon="pause" title={t('暂停全部', 'Pause all')} aria-label={t('暂停全部', 'Pause all')} tone="icon-only" onClick={() => run(() => host.command('tasks.pauseAll'))} /><Button icon="play" title={t('继续全部', 'Resume all')} aria-label={t('继续全部', 'Resume all')} tone="icon-only" onClick={() => run(() => host.command('tasks.resumeAll'))} /></div>
    <div className="workspace"><section className="list-panel panel"><div className="list-caption task-list-caption"><strong>{t('任务', 'Tasks')}</strong><div className="task-selection-controls"><label className="check" title={t('全选当前列表', 'Select all tasks in this list')}><input type="checkbox" aria-label={t('全选当前列表', 'Select all tasks in this list')} checked={ids.length > 0 && selection.length === ids.length} ref={element => { if (element) element.indeterminate = selection.length > 0 && selection.length < ids.length; }} disabled={!ids.length} onChange={event => setSelection(event.target.checked ? ids : [])} /><span>{t('全选', 'Select all')}</span></label><small aria-live="polite">{t(`已选 ${selection.length} 项`, `${selection.length} selected`)}</small><Button icon="trash" size="small" disabled={!selection.length} onClick={() => bulkDelete(selection)}>{t('批量删除', 'Delete selected')}</Button></div><span className="task-list-count">{ids.length} / {state.taskIds.length}</span></div><div className="task-table-heading task-columns"><span>{t('名称', 'Name')}</span><span>{t('阶段 / 进度', 'Stage / progress')}</span><span className="task-speed">{t('速度', 'Speed')}</span><span className="task-eta">{t('剩余', 'ETA')}</span><span className="task-size">{t('大小', 'Size')}</span></div>{ids.length ? <VirtualList ids={ids} selected={selected} selection={selection} onSelectionChange={setSelection} onSelect={selectInList} onContextMenu={openContext} onVisibleIdsChange={setVisibleIds} renderRow={row} label={t('下载任务列表', 'Download task list')} scrollKey="tasks" /> : <Empty title={t('没有匹配的任务', 'No matching tasks')} detail={view.taskSearch || view.taskStatus !== 'all' ? t('调整搜索或状态筛选。', 'Adjust search or status filters.') : t('在上方粘贴链接开始下载。', 'Paste a link above to start downloading.')} />}</section><TaskDetail task={tasks[selected]} media={state.media[state.mediaByTask[selected]]} availability={availability[state.mediaByTask[selected]]} hidden={Boolean(tasks[selected]) && hidden} action={action} run={run} t={t} dispatch={dispatch} /></div>
    {context && tasks[context.id] && <TaskContextMenu task={tasks[context.id]} media={state.media[state.mediaByTask[context.id]]} availability={availability[state.mediaByTask[context.id]]} position={context.position} trigger={context.trigger} close={closeContext} action={action} run={run} t={t} />}
  </div>;
}
const TaskDetail = memo(function TaskDetail({ task, media, availability, hidden, action, run, t, dispatch }: { task?: Task; media?: Media; availability?: MediaAvailability; hidden: boolean; action: (task: Task, action: string) => void; run: Run; t: Translate; dispatch: Dispatch<Action> }) {
  const [infoState, setInfoState] = useState({ id: '', busy: false, error: '' });
  const [infoRetry, setInfoRetry] = useState(0);
  const infoRequest = useRef<{ id: string; promise: Promise<Task> } | null>(null);
  const info = task?.mediaInfo || media?.mediaInfo;
  const unavailable = availability === 'missing' || availability === 'unknown';
  useEffect(() => {
    if (!task || task.status !== 'completed' || task.kind === 'assets' || info || unavailable) return;
    const id = task.id; let disposed = false;
    setInfoState({ id, busy: true, error: '' });
    if (infoRequest.current?.id !== id) infoRequest.current = { id, promise: host.command<Task>('tasks.info', { id }) };
    void infoRequest.current.promise.then(result => { if (!disposed) { dispatch({ type: 'event', event: { kind: 'task.upsert', data: result as unknown as Json } }); setInfoState({ id, busy: false, error: '' }); } }).catch(reason => { if (!disposed) setInfoState({ id, busy: false, error: String(reason) }); });
    return () => { disposed = true; };
  }, [task?.id, task?.status, info, infoRetry, unavailable, dispatch]);
  if (!task) return <aside className="detail panel"><Empty title={t('任务详情', 'Task details')} detail={t('选中任务以查看计划、进度和操作。', 'Select a task to see its plan, progress, and actions.')} icon="info" /></aside>;
  const progress = taskProgress(task);
  const controls = taskActions(task, t);
  return <aside className="detail panel" aria-label={t('任务详情', 'Task details')}><div className="detail-scroll"><div className="detail-top"><span className="eyebrow">{t('已选任务', 'Selected task')}</span><Badge tone={statusTone(task.status)}>{statusLabel(task.status, t)}</Badge></div><Thumbnail large url={task.hasThumbnail ? host.assetUrl(task.id, 'taskThumbnail') : media && host.assetUrl(media.id, 'thumbnail')} /><h2>{task.title || t('等待媒体信息', 'Waiting for media information')}</h2><code className="task-id">{task.id}</code>{hidden && <p className="muted">{t('所选任务被当前筛选隐藏。', 'Selected task is hidden by the current filter.')}</p>}
    <div className="detail-stage"><strong>{stepLabel(task)}{stageLabel(task.stage, task.status, t)}</strong><span>{progress.indeterminate ? t('等待阶段进度', 'Waiting for stage progress') : `${progress.value.toFixed(1)}%`}</span></div><Progress task={task} />{task.error && <p className="notice danger" role="alert">{explainError(task.error, t)}</p>}<FileAvailability availability={availability} t={t} />{task.warnings?.map(warning => <p key={warning} className="notice" role="status">{explainError(warning, t)}</p>)}
    {task.kind === 'assets' && <p className="muted">{t('附件保存任务', 'Attachment task')}</p>}{Boolean(task.files?.length) && <div className="task-files"><h3>{t('生成文件', 'Generated files')}</h3>{task.files?.map(file => <p key={file.filename} className="break-text">{file.filename}<small className="muted"> · {file.kind} · {(file.size / 1024).toFixed(1)} KiB</small></p>)}</div>}
    <div className="detail-actions">{controls.length > 0 && <div className="detail-task-controls">{controls.map(item => <Button key={item.action} icon={item.icon} tone={['resume', 'retry'].includes(item.action) ? 'primary' : ''} onClick={() => action(task, item.action)}>{item.label}</Button>)}</div>}<Button icon="info" onClick={() => action(task, 'logs')}>{t('查看日志', 'View logs')}</Button></div>
    <dl className="facts"><dt>{t('创建时间', 'Created')}</dt><dd>{new Date(task.createdAt).toLocaleString()}</dd><dt>{t('处理计划', 'Processing plan')}</dt><dd>{qualityLabel(task.options, t)} · {optionLabel(String(task.options.encoder || 'copy'), t)} · {optionLabel(String(task.options.subtitleLanguage || 'none'), t)}</dd><dt>{t('来源', 'Source')}</dt><dd className="break-text">{task.url}</dd><dt>{t('结果文件', 'Result file')}</dt><dd className="break-text">{task.outputPath || t('尚未保存', 'Not saved yet')}</dd></dl>
    {task.kind !== 'assets' && <><MediaInformation title={t('来源视频信息', 'Source video information')} info={task.sourceInfo} t={t} /><MediaInformation title={t('结果文件信息', 'Result file information')} info={info} size={task.size ?? media?.size} t={t} />{!info && infoState.id === task.id && infoState.busy && <p className="hint">{t('正在读取本地文件信息…', 'Reading local file information…')}</p>}{!info && infoState.id === task.id && infoState.error && <p className="hint" title={explainError(infoState.error, t)}>{t('暂时无法读取结果信息，原文件未修改。', 'Result information could not be read. The file is unchanged.')} <Button size="small" onClick={() => { infoRequest.current = null; setInfoRetry(value => value + 1); }}>{t('重试', 'Retry')}</Button></p>}</>}
    <details><summary>{t('更多操作', 'More actions')}</summary><div className="actions wrap">{['completed', 'retained'].includes(task.status) && media && <><Button icon="refresh" size="small" disabled={unavailable} onClick={() => action(task, 'convert')}>{t('转换格式', 'Convert format')}</Button>{task.kind !== 'assets' && (!info || Boolean(info.videoCodec || info.width)) && <Button icon="video" size="small" disabled={unavailable} onClick={() => action(task, 'compress')}>{t('压缩视频', 'Compress video')}</Button>}</>}<Button icon="copy" size="small" onClick={() => run(() => host.copyTask(task, 'title'))}>{t('复制视频名', 'Copy video title')}</Button><Button icon="copy" size="small" onClick={() => run(() => host.copyTask(task, 'source'))}>{t('复制来源', 'Copy source')}</Button>{host.desktop && <Button icon="copy" size="small" disabled={!media} onClick={() => run(() => host.copyTask(task, 'path'))}>{t('复制完整路径', 'Copy full path')}</Button>}<Button icon="trash" size="small" tone="danger" onClick={() => action(task, 'delete')}>{t('删除任务', 'Delete task')}</Button></div></details></div>
    <div className="detail-footer"><Button icon={host.desktop ? 'folder' : 'download'} tone="primary" disabled={!media || unavailable} onClick={() => media && run(() => host.result(media.id, host.desktop ? 'reveal' : 'download'))}>{host.desktop ? t('定位结果文件', 'Reveal result file') : t('下载结果文件', 'Download result file')}</Button></div>
  </aside>;
});
function MediaInformation({ title, info, size, t }: { title: string; info?: MediaInfo | null; size?: number | string | null; t: Translate }) {
  const fileSize = size ?? info?.size;
  return <section className="media-information" aria-label={title}><h3>{title}</h3>{info || fileSize ? <dl className="facts"><dt>{t('实际画质 / 尺寸', 'Actual quality / dimensions')}</dt><dd>{mediaQuality(info)} · {dimensions(info)}</dd><dt>{t('帧率', 'Frame rate')}</dt><dd>{frameRate(info?.fps)}</dd><dt>{t('视频 / 音频编码', 'Video / audio codecs')}</dt><dd>{info?.videoCodec || '—'} / {info?.audioCodec || '—'}</dd><dt>{t('容器格式', 'Container')}</dt><dd>{info?.container || '—'}</dd><dt>{t('大小 / 时长', 'Size / duration')}</dt><dd>{bytes(fileSize)} · {duration(info?.duration)}</dd></dl> : <p className="hint">{t('暂无可用信息', 'No information available')}</p>}</section>;
}
function Completed({ state, dispatch, select, action, cover, run, refresh, t }: { state: AppState; dispatch: Dispatch<Action>; select: (id: string) => void; action: (task: Task, action: string) => void; cover: (media: Media) => void; run: Run; refresh: () => Promise<void>; t: Translate }) {
  const ids = useMemo(() => state.mediaIds.filter(id => `${state.media[id].title} ${state.media[id].filename}`.toLowerCase().includes(state.view.mediaSearch.toLowerCase())).reverse(), [state.mediaIds, state.media, state.view.mediaSearch]);
  const selected = state.view.mediaSelection || ids[0]; const item = state.media[selected];
  const [visibleIds, setVisibleIds] = useState<string[]>([]);
  const availabilityIds = useMemo(() => [...visibleIds, ...(selected ? [selected] : [])], [visibleIds, selected]);
  const { availability, refresh: refreshAvailability } = useMediaAvailability(availabilityIds);
  const unavailable = availability[selected] === 'missing' || availability[selected] === 'unknown';
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
    if (!item || item.mediaInfo || unavailable) return;
    const id = item.taskId; let disposed = false;
    setInfoState({ id, busy: true, error: '' });
    if (infoRequest.current?.id !== id) infoRequest.current = { id, promise: host.command<Task>('tasks.info', { id }) };
    void infoRequest.current.promise.then(result => { if (!disposed) { if (result.mediaInfo) dispatch({ type: 'event', event: { kind: 'media.upsert', data: { id: item.id, mediaInfo: result.mediaInfo } } }); setInfoState({ id, busy: false, error: '' }); } }).catch(reason => { if (!disposed) setInfoState({ id, busy: false, error: String(reason) }); });
    return () => { disposed = true; };
  }, [item?.id, item?.taskId, item?.mediaInfo, infoRetry, unavailable, dispatch]);
  const row = useCallback((id: string) => <MediaRow media={state.media[id]} availability={availability[id]} t={t} />, [state.media, availability, t]);
  const contextMenu = context && state.media[context.id] && <TaskContextMenu task={state.tasks[context.task.id] || context.task} media={state.media[context.id]} availability={availability[context.id]} position={context.position} trigger={context.trigger} close={closeContext} action={action} run={run} t={t} />;
  return <div className="page"><Heading title={t('已完成', 'Completed media')} subtitle={t(`完整媒体库 · ${state.mediaIds.length} 个媒体`, `Full media library · ${state.mediaIds.length} media`)}><Button icon="refresh" onClick={() => run(async () => { await refresh(); refreshAvailability(); })}>{t('刷新', 'Refresh')}</Button></Heading><div className="toolbar"><input id="media-search" type="search" aria-label={t('搜索媒体库', 'Search media library')} value={state.view.mediaSearch} placeholder={t('搜索标题、文件名', 'Search title and filename')} onChange={event => dispatch({ type: 'view', patch: { mediaSearch: event.target.value } })} /></div><div className="workspace"><section className="panel list-panel"><div className="list-caption"><strong>{t('媒体库', 'Media library')}</strong><span>{ids.length} / {state.mediaIds.length}</span></div>{ids.length ? <VirtualList ids={ids} selected={selected} onSelect={select} onContextMenu={openContext} onVisibleIdsChange={setVisibleIds} renderRow={row} label={t('已完成媒体列表', 'Completed media list')} scrollKey="media" /> : <Empty title={t('没有匹配的媒体', 'No matching media')} detail={t('下载完成的媒体可在这里查看和准备封面。', 'View completed downloads and prepare covers here.')} icon="library" />}</section><aside className="detail panel">{item ? <><div className="detail-scroll"><span className="eyebrow">{t('已选媒体', 'Selected media')}</span><Thumbnail large url={host.assetUrl(item.id, 'thumbnail')} /><h2>{item.title}</h2><FileAvailability availability={availability[selected]} t={t} /><dl className="facts"><dt>{t('结果文件', 'Result file')}</dt><dd className="break-text">{item.filename}</dd><dt>{t('大小 / 时长', 'Size / duration')}</dt><dd>{bytes(item.size)} · {duration(item.duration)}</dd></dl><MediaInformation title={t('结果文件信息', 'Result file information')} info={item.mediaInfo} size={item.size} t={t} />{!item.mediaInfo && infoState.id === item.taskId && infoState.busy && <p className="hint">{t('正在读取本地文件信息…', 'Reading local file information…')}</p>}{!item.mediaInfo && infoState.id === item.taskId && infoState.error && <p className="hint" title={explainError(infoState.error, t)}>{t('暂时无法读取结果信息，原文件未修改。', 'Result information could not be read. The file is unchanged.')} <Button size="small" onClick={() => { infoRequest.current = null; setInfoRetry(value => value + 1); }}>{t('重试', 'Retry')}</Button></p>}<div className="actions wrap"><Button icon="image" disabled={unavailable} onClick={() => cover(item)}>{t('封面工作室', 'Cover studio')}</Button>{host.desktop && <Button icon="play" disabled={unavailable} onClick={() => run(() => host.result(item.id, 'open'))}>{t('打开媒体', 'Open media')}</Button>}</div></div><div className="detail-footer"><Button tone="primary" disabled={unavailable} icon={host.desktop ? 'folder' : 'download'} onClick={() => run(() => host.result(item.id, host.desktop ? 'reveal' : 'download'))}>{host.desktop ? t('定位结果文件', 'Reveal result file') : t('下载结果文件', 'Download result file')}</Button></div></> : <Empty title={t('媒体详情', 'Media details')} detail={t('选择媒体查看文件操作。', 'Select media to view file actions.')} icon="library" />}</aside></div>{contextMenu}</div>;
}
const MediaRow = memo(function MediaRow({ media, availability, t }: { media: Media; availability?: MediaAvailability; t: Translate }) { const quality = mediaQualityWithFps(media.mediaInfo); return <div className="media-row-content"><Thumbnail url={host.assetUrl(media.id, 'thumbnail')} /><div className="row-main"><div className="row-heading"><strong className="truncate">{media.title}</strong><Badge tone={availability === 'missing' || availability === 'unknown' ? '' : 'success'}>{availabilityLabel(availability, t) || t('已完成', 'Completed')}{quality !== '—' && <> · {quality}</>}</Badge></div><div className="row-meta"><span className="truncate">{media.filename}</span><span>{bytes(media.size)} · {duration(media.duration)}</span></div></div></div>; });
function QuickOptions({ initial, apply, close, t }: { initial: DownloadOptions; apply: (value: DownloadOptions) => Promise<void>; close: () => void; t: Translate }) { const [options, setOptions] = useState(initial); const [busy, setBusy] = useState(false); const [error, setError] = useState(''); const change = useCallback((key: string, value: DownloadOptions[string]) => setOptions(current => ({ ...current, [key]: value })), []); return <Modal title={t('下一批次下载方案', 'Next batch download plan')} subtitle={t('已加入任务的方案已固定。', 'Existing tasks keep their saved plans.')} t={t} close={() => { if (!busy) close(); }} footer={<><Button disabled={busy} onClick={close}>{t('取消', 'Cancel')}</Button><Button tone="primary" disabled={busy} onClick={() => { setBusy(true); void apply(options).catch(reason => setError(String(reason))).finally(() => setBusy(false)); }}>{busy ? t('保存中…', 'Saving…') : t('保存批次方案', 'Save batch plan')}</Button></>}>{error && <p className="notice danger" role="alert">{explainError(error, t)}</p>}<OptionsForm options={options} change={change} t={t} quick /></Modal>; }
function RequirementsDialog({ task, t, close, restarted }: { task: Task; t: Translate; close: () => void; restarted: () => void }) {
  const [options, setOptions] = useState({ ...defaults, ...task.options }); const [busy, setBusy] = useState(false); const [error, setError] = useState('');
  const submit = async () => { setBusy(true); setError(''); try { await host.command('tasks.retryCurrent', { id: task.id, options: commandOptions({ ...options, collectionMode: 'single' }) }); restarted(); } catch (reason) { setError(String(reason)); } finally { setBusy(false); } };
  return <Modal title={t('调整要求重试', 'Adjust requirements and retry')} subtitle={task.title} t={t} close={() => { if (!busy) close(); }} footer={<><Button disabled={busy} onClick={close}>{t('取消', 'Cancel')}</Button><Button tone="primary" disabled={busy} onClick={() => void submit()}>{t('使用这些要求重试', 'Retry with these requirements')}</Button></>}><p className="notice">{t('来源不满足已选要求。请明确调整需要放宽的项目，或取消。未更改的要求仍须满足；原结果文件保留。', 'The source does not meet the selected requirements. Adjust the requirements you accept relaxing, or cancel. Unchanged requirements must still be met; existing result files are kept.')}</p><p className="notice danger break-text">{explainError(error || task.error || '', t)}</p><OptionsForm options={options} change={(key, value) => setOptions(current => ({ ...current, [key]: value }))} t={t} /></Modal>;
}
function DeleteDialog({ tasks, batch = false, initialFiles, remember, close, refresh, run, t }: { tasks: Task[]; batch?: boolean; initialFiles: boolean; remember: (files: boolean) => Promise<void>; close: () => void; refresh: () => Promise<void>; run: Run; t: Translate }) {
  const [files, setFiles] = useState(initialFiles); const [busy, setBusy] = useState(false); const [saving, setSaving] = useState(false); const [error, setError] = useState('');
  const [remaining, setRemaining] = useState(tasks);
  const changeFiles = (next: boolean) => {
    setFiles(next); setSaving(true); setError('');
    void remember(next).catch(reason => { setFiles(!next); setError(String(reason)); }).finally(() => setSaving(false));
  };
  const remove = () => { setBusy(true); setError(''); run(async () => {
    const pending = new Map(remaining.map(task => [task.id, task]));
    try {
      if (!batch) { await host.command('tasks.delete', { id: remaining[0].id, deleteFiles: files }); pending.clear(); }
      else {
        const failures: string[] = [];
        for (let offset = 0; offset < remaining.length; offset += 1000) {
          const report = await host.command<{ results: { id: string; ok: boolean; error?: string }[] }>('tasks.deleteBatch', { ids: remaining.slice(offset, offset + 1000).map(task => task.id), deleteFiles: files });
          for (const result of report.results) {
            if (result.ok) pending.delete(result.id);
            else failures.push(`${pending.get(result.id)?.title || result.id}: ${result.error || t('删除失败', 'Delete failed')}`);
          }
        }
        if (failures.length) setError(failures.join('\n'));
      }
    } catch (reason) { setError(String(reason)); }
    finally {
      setRemaining([...pending.values()]);
      try { await refresh(); } finally { setBusy(false); }
    }
    if (!pending.size) close();
  }); };
  const task = remaining[0] || tasks[0];
  return <Modal title={batch ? t('批量删除任务', 'Delete selected tasks') : t('删除任务', 'Delete task')} t={t} close={() => { if (!busy && !saving) close(); }} footer={<><Button onClick={close} disabled={busy || saving}>{t('取消', 'Cancel')}</Button><Button tone="danger" disabled={busy || saving || !remaining.length} onClick={remove}>{busy ? t('删除中…', 'Deleting…') : t('删除', 'Delete')}</Button></>}>
    <p className="dialog-copy">{batch ? t(`删除选中的 ${remaining.length} 项任务？正在运行的任务会先停止。`, `Delete ${remaining.length} selected tasks? Active work stops first.`) : t(`删除“${task.title || task.id}”？正在运行的任务会先停止。`, `Delete “${task.title || task.id}”? Active work stops first.`)}</p>
    <Check label={batch ? t('同时删除所选任务管理的媒体文件', 'Also delete media files managed by selected tasks') : t('同时删除此任务管理的媒体文件', 'Also delete media files managed by this task')} checked={files} disabled={busy || saving} onChange={event => changeFiles(event.target.checked)} />
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
function About({ version, release, busy, error, check, t, run, appUpdate }: { version: string; release: ReleaseStatus | null; busy: boolean; error: string; check: () => Promise<void>; t: Translate; run: Run; appUpdate?: AppUpdateController }) { return <div className="page"><Heading title={t('关于', 'About')} subtitle={t('版本与内置组件。', 'Version and included components.')} /><div className="page-scroll about-content"><section className="panel about-identity"><div className="brand-mark"><Icon name="download" /></div><div className="about-identity-copy"><div className="about-title"><h2>Huifa Media Downloader</h2><a className="about-project" href="https://github.com/jwwsjlm/huifa-media-downloader" target="_blank" rel="noopener noreferrer" title={t('打开 GitHub 项目', 'Open GitHub project')} onClick={event => { event.preventDefault(); run(() => host.openProject()); }}><Icon name="external" /><span>github.com/jwwsjlm/huifa-media-downloader</span></a></div><p className="muted">{t('版本', 'Version')} {version} · Rust / {host.desktop ? 'Tauri' : 'Web'} / React</p><p>{t('媒体下载、离线处理与封面准备，一个连续的媒体工作空间。', 'A continuous workspace for media downloads, offline processing, and cover preparation.')}</p></div></section>{appUpdate ? <AppUpdatePanel update={appUpdate} t={t} /> : <section className="panel settings-group about-update"><h2>{t('版本更新', 'Version updates')}</h2><p>{t('当前版本', 'Current version')} {version}{release && <> · {t('云端版本', 'Latest release')} {release.latestVersion}</>}</p><div className="actions wrap"><Button icon="refresh" disabled={busy} onClick={() => run(check)}>{busy ? t('检查中…', 'Checking…') : t('检查新版本', 'Check for updates')}</Button>{release?.updateAvailable && <Button icon="external" tone="primary" onClick={() => run(() => host.openRelease(release.url))}>{t('打开新版下载页面', 'Open new release page')}</Button>}</div>{error && <p className="hint" role="status"><span>{releaseCheckHint(error, t)}</span><br /><span className="break-text">{error}</span></p>}{release && !release.updateAvailable && !error && <p className="hint">{t('当前已是最新正式版本。', 'You have the latest stable release.')}</p>}{!host.desktop && <p className="hint">{t('Docker 请拉取新版镜像并重新创建容器，保留数据挂载。', 'For Docker, pull the new image and recreate the container, preserving data mounts.')}</p>}{host.desktop && <p className="hint">{t('此软件包请手动更新；AppImage 支持应用内更新。', 'Update this package manually. AppImage supports in-app updates.')}</p>}</section>}<section className="panel settings-group"><h2>{t('内置组件', 'Included components')}</h2>{[['yt-dlp', t('跨站媒体信息提取与下载。', 'Cross-site media extraction and downloading.'), 'Unlicense'], ['FFmpeg / FFprobe', t('流合并、媒体探测和转码。', 'Stream merging, media probing, and transcoding.'), t('上游许可证', 'Upstream licenses')], ['QuickJS-NG / yt-dlp-ejs', t('处理来源网站的 JavaScript。', 'JavaScript support for source websites.'), 'MIT / Unlicense'], ['Rust / Tauri / React', t('共享业务核心与桌面、网页界面。', 'Shared business core with desktop and web interfaces.'), 'MIT / Apache-2.0']].map(([name, detail, license]) => <div key={name} className="dependency-row"><strong>{name}</strong><span className="muted">{detail}</span><small>{license}</small></div>)}</section></div></div>; }
