import { Fragment, useCallback, useEffect, useRef, useState } from 'react';
import { Badge, Button, Modal } from './components';
import { optionGroups } from './options';
import { stageLabel, stepLabel, statusLabel, statusTone } from './task';
import { host } from './transport';
import type { Translate } from './i18n';
import type { Task, TaskLogs } from './types';

export function TaskLogsDialog({ task, t, close }: { task: Task; t: Translate; close: () => void }) {
  const [logs, setLogs] = useState<TaskLogs | null>(null); const [total, setTotal] = useState(0);
  const [busy, setBusy] = useState(true); const [error, setError] = useState('');
  const [copying, setCopying] = useState(false); const [copied, setCopied] = useState(false);
  const alive = useRef(true); const pending = useRef<Promise<unknown> | null>(null); const copyingRef = useRef(false);
  const following = useRef(true); const active = useRef(false); const taskId = useRef(task.id); const loadedId = useRef('');
  const wasActive = useRef(false);
  const logView = useRef<HTMLPreElement>(null); const scrollBottom = useRef(true);
  taskId.current = task.id;
  active.current = ['queued', 'probing', 'downloading', 'waiting_processing', 'processing'].includes(task.status);
  const filters = optionGroups.flatMap(group => group.fields).filter(field => ['liveFilter', 'dateAfter', 'dateBefore', 'durationMin', 'durationMax'].includes(field.key) && Boolean(task.options[field.key])).map(field => {
    const raw = String(task.options[field.key]); const choice = field.choices?.find(([value]) => value === raw);
    return { key: field.key, label: t(field.zh, field.en), value: choice ? t(choice[1], choice[2]) : raw };
  });
  const load = useCallback((cursor: { before?: number; offset?: number } = {}, display = true): Promise<unknown> => {
    if (pending.current || copyingRef.current) return pending.current || Promise.resolve();
    setBusy(true);
    const request = host.command<TaskLogs>('tasks.logs', { id: task.id, limit: 65536, ...cursor }).then(value => {
      if (!alive.current || taskId.current !== task.id) return;
      setTotal(value.total); setError('');
      if (display) {
        following.current = value.end >= value.total; loadedId.current = task.id;
        setLogs(value); setCopied(false);
      }
    }).catch(reason => { if (alive.current && taskId.current === task.id) setError(reason instanceof Error ? reason.message : String(reason)); }).finally(() => {
      if (pending.current === request) pending.current = null;
      if (alive.current && taskId.current === task.id) setBusy(false);
    });
    pending.current = request;
    return request;
  }, [task.id]);
  useEffect(() => {
    alive.current = true;
    void (async () => { await pending.current; if (alive.current && loadedId.current !== task.id) await load(); })();
    const timer = window.setInterval(() => { if (active.current && !pending.current && !copyingRef.current) void load({}, following.current); }, 2000);
    return () => { alive.current = false; copyingRef.current = false; window.clearInterval(timer); };
  }, [load, task.id]);
  useEffect(() => {
    if (wasActive.current && !active.current) void (async () => { await pending.current; if (alive.current && !copyingRef.current) await load({}, following.current); })();
    wasActive.current = active.current;
  }, [task.status, load]);
  useEffect(() => { const view = logView.current; if (view && following.current && scrollBottom.current) view.scrollTop = view.scrollHeight; }, [logs]);
  const copy = async () => {
    copyingRef.current = true; setCopying(true); setCopied(false); setError('');
    try {
      if (!navigator.clipboard?.writeText) throw new Error(t('无法复制，请在日志中选中文本后手动复制。', 'Cannot copy. Select the log text and copy it manually.'));
      await pending.current;
      const chunks: string[] = []; const end = total; let offset = 0;
      while (offset < end && copyingRef.current && alive.current) {
        const request = host.command<TaskLogs>('tasks.logs', { id: task.id, offset, limit: Math.max(4, Math.min(262144, end - offset)) });
        pending.current = request;
        let page: TaskLogs;
        try { page = await request; } finally { if (pending.current === request) pending.current = null; }
        if (!copyingRef.current || !alive.current) return;
        if (page.end <= offset || page.start !== offset) throw new Error(t('日志读取中断，请重试复制。', 'Log reading was interrupted. Try copying again.'));
        chunks.push(page.end > end ? new TextDecoder().decode(new TextEncoder().encode(page.text).subarray(0, end - offset)) : page.text); offset = page.end;
      }
      if (!copyingRef.current || !alive.current) return;
      await navigator.clipboard.writeText(chunks.join(''));
      if (alive.current) setCopied(true);
    } catch (reason) { if (alive.current) setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { copyingRef.current = false; if (alive.current) setCopying(false); }
  };
  const latest = () => { scrollBottom.current = true; void load(); };
  return <Modal title={t('任务日志', 'Task logs')} subtitle={task.title || task.url} t={t} wide close={close} footer={<>{copying ? <Button onClick={() => { copyingRef.current = false; }}>{t('取消复制', 'Cancel copying')}</Button> : <Button icon="copy" disabled={busy || !total} onClick={() => void copy()}>{copied ? t('已复制', 'Copied') : t('复制完整日志', 'Copy full log')}</Button>}<Button icon="refresh" disabled={busy || copying} onClick={latest}>{t('刷新', 'Refresh')}</Button><Button onClick={close}>{t('关闭', 'Close')}</Button></>}>
    <div className="task-log-summary"><Badge tone={statusTone(task.status)}>{statusLabel(task.status, t)}</Badge><span>{stepLabel(task)}{stageLabel(task.stage, task.status, t)}</span><code>{task.id}</code><span className="muted">{active.current ? t('每 2 秒自动刷新', 'Refreshes automatically every 2 seconds') : t('任务已停止', 'Task is stopped')}</span></div>
    {filters.length > 0 && <section aria-label={t('任务筛选条件', 'Task filters')}><h3>{t('任务筛选条件', 'Task filters')}</h3><dl className="facts">{filters.map(field => <Fragment key={field.key}><dt>{field.label}</dt><dd>{field.value}</dd></Fragment>)}</dl></section>}
    <p className="hint">{t('原始日志可能包含 Cookie、临时地址或本地路径，转发前请检查。', 'Raw logs may contain cookies, temporary URLs, or local paths. Check before sharing.')}</p>
    {error && <p className="notice danger" role="alert">{error}</p>}
    {busy && !logs && <p className="muted" role="status">{t('正在读取日志…', 'Loading logs…')}</p>}
    {logs && <><div className="task-log-navigation"><Button size="small" disabled={busy || copying || logs.start === 0} onClick={() => { scrollBottom.current = false; void load({ before: logs.start }); }}>{t('较早日志', 'Older logs')}</Button><Button size="small" disabled={busy || copying || logs.end >= total} onClick={() => { scrollBottom.current = false; void load({ offset: logs.end }); }}>{t('较新日志', 'Newer logs')}</Button><Button size="small" disabled={busy || copying || following.current} onClick={latest}>{t('最新日志', 'Latest logs')}</Button><span className="hint">{t('字节 {start}–{end} / {total}', 'Bytes {start}–{end} / {total}').replace('{start}', String(logs.start)).replace('{end}', String(logs.end)).replace('{total}', String(total))}</span></div>{logs.text ? <pre ref={logView} className="task-logs" tabIndex={0} aria-label={t('任务日志内容', 'Task log entries')} onScroll={event => { const view = event.currentTarget; scrollBottom.current = view.scrollHeight - view.scrollTop - view.clientHeight < 40; }}>{logs.text}</pre> : <p className="muted">{t('此任务暂无已记录的日志。', 'No logs have been recorded for this task.')}</p>}{logs.legacy && <p className="hint">{t('此任务执行时尚未记录原始输出，现有历史仅供参考；重新执行后会保存完整日志。', 'Raw output was not recorded for this run. Existing history is limited; another run will save the full log.')}</p>}</>}
  </Modal>;
}
