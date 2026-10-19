import { useLayoutEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Icon } from './components';
import { taskActions } from './task';
import { host } from './transport';
import type { Translate } from './i18n';
import type { Run } from './App';
import type { Media, Task } from './types';
import type { MediaAvailability } from './useMediaAvailability';

export function TaskContextMenu({ task, media, availability, position, trigger, close, action, run, t }: {
  task: Task; media?: Media; position: { x: number; y: number }; trigger: HTMLElement;
  close: () => void; action: (task: Task, action: string) => void; run: Run; t: Translate; availability?: MediaAvailability;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [location, setLocation] = useState(position);
  useLayoutEffect(() => {
    const menu = ref.current!; const bounds = menu.getBoundingClientRect();
    setLocation({ x: Math.max(8, Math.min(position.x, innerWidth - bounds.width - 8)), y: Math.max(8, Math.min(position.y, innerHeight - bounds.height - 8)) });
    menu.querySelector<HTMLButtonElement>('[role=menuitem]:not(:disabled)')?.focus();
    const outside = (event: PointerEvent) => { if (!menu.contains(event.target as Node)) close(); };
    const changed = (event: Event) => { if (!menu.contains(event.target as Node)) close(); };
    document.addEventListener('pointerdown', outside); document.addEventListener('scroll', changed, true); window.addEventListener('resize', close); window.addEventListener('blur', close);
    return () => {
      document.removeEventListener('pointerdown', outside); document.removeEventListener('scroll', changed, true); window.removeEventListener('resize', close); window.removeEventListener('blur', close);
      if (menu.contains(document.activeElement) || document.activeElement === document.body) trigger.isConnected && trigger.focus({ preventScroll: true });
    };
  }, [position.x, position.y, trigger, close]);
  const perform = (operation: () => void) => { close(); operation(); };
  const unavailable = availability === 'missing' || availability === 'unknown';
  const info = task.mediaInfo || media?.mediaInfo;
  const video = task.kind !== 'assets' && (!info || Boolean(info.videoCodec || info.width));
  return createPortal(<div ref={ref} role="menu" aria-label={t('任务操作', 'Task actions')} className="task-context-menu" style={{ left: location.x, top: location.y }} onContextMenu={event => event.preventDefault()} onKeyDown={event => {
    if (event.key === 'Escape' || event.key === 'Tab') { if (event.key === 'Escape') event.preventDefault(); close(); return; }
    if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault(); const items = [...event.currentTarget.querySelectorAll<HTMLButtonElement>('[role=menuitem]:not(:disabled)')]; const index = items.indexOf(document.activeElement as HTMLButtonElement);
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1 : (index + (event.key === 'ArrowUp' ? -1 : 1) + items.length) % items.length;
    items[next]?.focus();
  }}>
    <span className="task-menu-title truncate">{task.title || task.url}</span>
    {taskActions(task, t).map(item => <button type="button" role="menuitem" tabIndex={-1} key={item.action} onClick={() => perform(() => action(task, item.action))}><Icon name={item.icon} />{item.label}</button>)}
    <button type="button" role="menuitem" tabIndex={-1} onClick={() => perform(() => action(task, 'logs'))}><Icon name="info" />{t('查看日志', 'View logs')}</button>
    {media && <button type="button" role="menuitem" tabIndex={-1} disabled={unavailable} onClick={() => perform(() => run(() => host.result(media.id, host.desktop ? 'reveal' : 'download')))}><Icon name={host.desktop ? 'folder' : 'download'} />{host.desktop ? t('定位结果文件', 'Reveal result file') : t('下载结果文件', 'Download result file')}</button>}
    {media && ['completed', 'retained'].includes(task.status) && <><button type="button" role="menuitem" tabIndex={-1} disabled={unavailable} onClick={() => perform(() => action(task, 'convert'))}><Icon name="refresh" />{t('转换格式', 'Convert format')}</button>{video && <button type="button" role="menuitem" tabIndex={-1} disabled={unavailable} onClick={() => perform(() => action(task, 'compress'))}><Icon name="video" />{t('压缩视频', 'Compress video')}</button>}</>}
    <button type="button" role="menuitem" tabIndex={-1} onClick={() => perform(() => run(() => host.copyTask(task, 'title')))}><Icon name="copy" />{t('复制视频名', 'Copy video title')}</button>
    <button type="button" role="menuitem" tabIndex={-1} onClick={() => perform(() => run(() => host.copyTask(task, 'source')))}><Icon name="copy" />{t('复制来源', 'Copy source')}</button>
    {host.desktop && <button type="button" role="menuitem" tabIndex={-1} disabled={!media} onClick={() => perform(() => run(() => host.copyTask(task, 'path')))}><Icon name="copy" />{t('复制完整路径', 'Copy full path')}</button>}
    <div role="separator" />
    <button type="button" role="menuitem" tabIndex={-1} className="danger" onClick={() => perform(() => action(task, 'delete'))}><Icon name="trash" />{t('删除任务', 'Delete task')}</button>
  </div>, document.body);
}
