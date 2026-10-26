import { useCallback, useEffect, useRef, useState } from 'react';
import { Button, Modal } from './components';
import { host } from './transport';
import { bytes } from './task';
import type { Translate } from './i18n';
import type { UpdateStatus } from './types';

export function useAppUpdate(enabled: boolean) {
  const [status, setStatus] = useState<UpdateStatus | null>(null);
  const [busy, setBusy] = useState(false); const [error, setError] = useState('');
  const [checked, setChecked] = useState(false); const [confirm, setConfirm] = useState(false);
  const [notice, setNotice] = useState('');
  const running = useRef(false);
  const receive = useCallback((next: UpdateStatus) => setStatus(current => !current || next.revision >= current.revision ? next : current), []);
  const call = useCallback(async (operation: () => Promise<UpdateStatus | void>) => {
    if (running.current) return;
    running.current = true; setBusy(true); setError('');
    try { const next = await operation(); if (next) receive(next); }
    catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { running.current = false; setBusy(false); }
  }, [receive]);
  const check = useCallback(async () => { await call(() => host.checkUpdate()); setChecked(true); }, [call]);
  useEffect(() => {
    if (!enabled) return;
    let disposed = false; let cleanup: (() => void) | undefined;
    void (async () => {
      try {
        const unsubscribe = await host.subscribeUpdate(next => { if (!disposed) receive(next); });
        if (disposed) { unsubscribe(); return; }
        cleanup = unsubscribe;
        const initial = await host.updateStatus();
        if (disposed) return;
        receive(initial);
        if (initial.error) setNotice(initial.error);
        if (!initial.restartRequired) await check();
      } catch (reason) { if (!disposed) setError(String(reason)); }
    })();
    return () => { disposed = true; cleanup?.(); };
  }, [enabled, check, receive]);
  return {
    status, busy: busy || Boolean(status && ['checking', 'downloading', 'verifying', 'installing'].includes(status.phase)),
    error: error || status?.error || '', notice, checked, confirm, setConfirm,
    check, download: () => call(() => host.downloadUpdate()), install: () => call(() => host.installUpdate()),
  };
}

export type AppUpdateController = ReturnType<typeof useAppUpdate>;

export function AppUpdatePanel({ update, t }: { update: AppUpdateController; t: Translate }) {
  const { status, busy, error } = update;
  const downloading = status?.phase === 'downloading';
  const verifying = status?.phase === 'verifying';
  const total = status?.totalBytes && status.totalBytes > 0 ? status.totalBytes : undefined;
  const percent = total && status ? Math.min(100, status.downloadedBytes / total * 100) : undefined;
  return <section className="settings-group panel app-update" aria-label={t('程序更新', 'Application update')}>
    <div className="section-heading"><h2>{t('程序更新', 'Application update')}</h2><Button icon="refresh" disabled={busy || status?.restartRequired} onClick={() => { void update.check(); }}>{status?.phase === 'checking' ? t('检查中…', 'Checking…') : t('检查程序更新', 'Check application update')}</Button></div>
    <p className="muted">{t('下载后由你选择重启时间。安装前自动保存任务并备份配置。', 'Choose when to restart after downloading. Tasks and configuration are backed up before installation.')}</p>
    {update.notice && update.notice !== error && <p className="notice" role="status">{update.notice}</p>}
    {status?.version && <div className="update-release"><strong>{t(`可用版本 ${status.version}`, `Available version ${status.version}`)}</strong>
      {status.canDownload && <Button icon="download" tone="primary" disabled={busy || status.restartRequired} onClick={() => { void update.download(); }}>{t('下载更新', 'Download update')}</Button>}
      {status.canInstall && <Button icon="refresh" tone="primary" disabled={busy || status.restartRequired} onClick={() => update.setConfirm(true)}>{t('重启并安装', 'Restart and install')}</Button>}
      {status.body && <details><summary>{t('更新说明', 'Release notes')}</summary><pre className="release-notes">{status.body}</pre></details>}
    </div>}
    {(downloading || verifying) && <div className="update-progress" role="status">
      <p>{verifying ? t('正在验证签名…', 'Verifying signature…') : t('正在下载更新…', 'Downloading update…')} {bytes(status.downloadedBytes)}{total ? ` / ${bytes(total)} (${Math.floor(percent!)}%)` : ''}</p>
      <progress aria-label={t('更新下载进度', 'Update download progress')} max={100} value={verifying ? undefined : percent} />
    </div>}
    {status?.phase === 'ready' && <p className="notice" role="status">{t('更新已下载并验证，可稍后重启安装。正常退出会保留更新包，下次先联网核对清单。', 'Update downloaded and verified. Restart when ready. The package is kept after quitting; reopening first checks the online manifest.')}</p>}
    {update.checked && status?.phase === 'idle' && !busy && !error && <p className="hint" role="status">{t('当前已是最新正式版本。', 'You have the latest stable release.')}</p>}
    {error && <p className="notice danger break-text" role="alert">{error}</p>}
  </section>;
}

export function AppUpdateDialog({ update, t }: { update: AppUpdateController; t: Translate }) {
  const stopped = update.status?.restartRequired && update.status.phase === 'error';
  if (!update.confirm && !stopped) return null;
  return <Modal title={stopped ? t('更新未完成', 'Update did not finish') : t('重启并安装更新', 'Restart and install update')} t={t}
    close={() => { if (!update.busy && !stopped) update.setConfirm(false); }}
    footer={stopped ? <p>{t('请关闭应用后重新打开，再重试更新。', 'Close and reopen the application before retrying the update.')}</p> : <>
      <Button disabled={update.busy} onClick={() => update.setConfirm(false)}>{t('稍后', 'Later')}</Button>
      <Button tone="primary" disabled={update.busy || !update.status?.canInstall} onClick={() => { void update.install(); }}>{update.status?.phase === 'installing' ? t('正在保存并安装…', 'Saving and installing…') : t('重启并安装', 'Restart and install')}</Button>
    </>}>
    <p>{stopped ? t('后台任务已停止，程序无法继续接受新任务。', 'Background work has stopped. The application cannot accept new tasks.') : t(`安装版本 ${update.status?.version}？`, `Install version ${update.status?.version}?`)}</p>
    {!stopped && <p className="muted">{t('立即停止后台工作并保存进度；安装完成后自动启动新版。直播会中断，媒体处理可能需要重新开始。', 'Background work stops and progress is saved immediately. The new version starts after installation. Live captures are interrupted; media processing may need to restart.')}</p>}
    {update.error && <p className="notice danger break-text" role="alert">{update.error}</p>}
  </Modal>;
}
