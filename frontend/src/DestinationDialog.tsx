import { useState } from 'react';
import { Button, Field, Modal } from './components';
import { host } from './transport';
import { explainError } from './errors';
import type { Translate } from './i18n';
import type { Task } from './types';

export function DestinationDialog({ task, t, close }: { task: Task; t: Translate; close: () => void }) {
  const [filename, setFilename] = useState(task.destinationConflict?.filename || '');
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const resolve = async (choice: string) => {
    setBusy(true); setError('');
    try { await host.command('tasks.destination', { id: task.id, choice, ...(choice === 'rename' ? { filename } : {}), ...(choice === 'overwrite' ? { confirmed: true } : {}) }); close(); }
    catch (reason) { setError(explainError(String(reason), t)); }
    finally { setBusy(false); }
  };
  return <Modal title={t('同名文件处理', 'Resolve filename conflict')} t={t} close={close} footer={<>
    <Button disabled={busy} onClick={() => void resolve('skip')}>{t('跳过任务', 'Skip task')}</Button>
    <Button disabled={busy} onClick={() => setConfirm(true)}>{t('覆盖已有文件…', 'Replace existing files…')}</Button>
    <Button tone="primary" disabled={busy || !filename.trim() || filename === task.destinationConflict?.filename} onClick={() => void resolve('rename')}>{t('改名后下载', 'Download with new name')}</Button>
  </>}>
    <p>{t('不同来源使用了相同文件名，请在下载前选择处理方式。', 'A different source uses this filename. Choose what to do before downloading.')}</p>
    <p className="notice break-text">{task.destinationConflict?.files.join(' · ')}</p>
    <Field label={t('新文件名', 'New filename')}><input value={filename} onChange={event => { setFilename(event.target.value); setConfirm(false); }} disabled={busy} maxLength={220} /></Field>
    {confirm && <div className="notice danger" role="alert"><p>{t('确认替换上面列出的已有文件？新结果通过验证后才替换；失败或取消会保留旧文件。被替换的旧任务将标记结果已替换。', 'Replace the existing files listed above? Replacement happens only after validation. Failure or cancellation keeps the old files. Previous tasks will indicate their results were replaced.')}</p><Button tone="danger" disabled={busy} onClick={() => void resolve('overwrite')}>{t('确认覆盖并下载', 'Confirm replacement and download')}</Button></div>}
    {error && <p className="notice danger" role="alert">{error}</p>}
  </Modal>;
}
