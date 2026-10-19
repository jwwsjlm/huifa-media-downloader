import { useEffect, useId, useState } from 'react';
import { Button } from './components';
import { host } from './transport';
import { bytes } from './task';
import { explainError } from './errors';
import type { Translate } from './i18n';
import './StoragePanel.css';

type Storage = { dataFree: number; downloadFree: number; processingFree: number; completedCacheBytes: number; recoverableCacheBytes: number; unusedToolGroups: number };
export function StoragePanel({ t }: { t: Translate }) {
  const titleId = useId();
  const [value, setValue] = useState<Storage | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const refresh = async (clean = false) => {
    setBusy(true); setError('');
    try { if (clean) { const result = await host.command<{ storage: Storage; failedTaskIds: string[] }>('storage.cleanup'); setValue(result.storage); if (result.failedTaskIds.length) setError(t('部分缓存未能清理，恢复所需文件已保留。', 'Some caches could not be cleaned; recovery files were preserved.')); }
      else setValue(await host.command<Storage>('storage.status')); }
    catch (reason) { setError(explainError(String(reason), t)); }
    finally { setBusy(false); }
  };
  useEffect(() => { void refresh(); }, []);
  return <section className="settings-group panel storage-panel" aria-labelledby={titleId} aria-busy={busy}><div className="section-heading"><h2 id={titleId}>{t('磁盘与缓存', 'Storage and cache')}</h2><Button icon="refresh" size="small" disabled={busy} onClick={() => void refresh()}>{t('刷新', 'Refresh')}</Button></div>
    {value ? <>
      <dl className="storage-volumes">
        <div><dt>{t('应用数据盘可用', 'Data volume available')}</dt><dd>{value.dataFree === 0 ? '0 B' : bytes(value.dataFree)}</dd></div>
        <div><dt>{t('下载目录所在盘可用', 'Download volume available')}</dt><dd>{value.downloadFree === 0 ? '0 B' : bytes(value.downloadFree)}</dd></div>
        <div><dt>{t('处理临时目录所在盘可用', 'Processing volume available')}</dt><dd>{value.processingFree === 0 ? '0 B' : bytes(value.processingFree)}</dd></div>
      </dl>
      <div className="storage-cache-row"><dl className="storage-cache-counts">
        <div><dt>{t('已完成任务缓存', 'Completed task cache')}</dt><dd>{value.completedCacheBytes === 0 ? '0 B' : bytes(value.completedCacheBytes)}</dd></div>
        <div><dt>{t('暂停与失败等任务缓存', 'Unfinished task cache')}</dt><dd>{value.recoverableCacheBytes === 0 ? '0 B' : bytes(value.recoverableCacheBytes)}</dd></div>
      </dl><Button icon="trash" size="small" disabled={busy} onClick={() => void refresh(true)}>{t('清理已完成缓存和未引用的旧组件', 'Clean completed caches and unused runtimes')}{value.unusedToolGroups ? ` (${value.unusedToolGroups})` : ''}</Button></div>
    </> : busy && <p className="hint" role="status">{t('正在读取磁盘与缓存…', 'Reading storage and cache…')}</p>}
    <p className="hint storage-retention">{t('成功后自动清理来源和处理中间文件，保留成品、封面预览和日志。暂停或失败的任务保留恢复文件。', 'Successful tasks automatically remove source and processing caches while keeping results, previews and logs. Unfinished tasks keep recovery files.')}</p>
    {error && <p className="notice danger" role="alert">{error}</p>}
  </section>;
}
