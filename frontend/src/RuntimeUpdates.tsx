import { useCallback, useEffect, useRef, useState } from 'react';
import { Button } from './components';
import { explainError } from './errors';
import { host } from './transport';
import type { Translate } from './i18n';
import type { Tool, ToolRelease } from './types';

export function useRuntimeUpdates(enabled: boolean, tools: Tool[], ffmpegMode: string) {
  const [releases, setReleases] = useState<ToolRelease[]>([]);
  const [errors, setErrors] = useState<string[]>([]);
  const [busy, setBusy] = useState('');
  const [checked, setChecked] = useState(false);
  const inputs = useRef({ enabled, tools, ffmpegMode }); inputs.current = { enabled, tools, ffmpegMode };
  const started = useRef(false); const running = useRef(false); const mounted = useRef(false);
  const request = useRef<AbortController | null>(null);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; request.current?.abort(); }; }, []);
  const check = useCallback(async () => {
    if (!inputs.current.enabled || running.current) return;
    running.current = true; setBusy('check'); setErrors([]);
    const controller = new AbortController(); request.current = controller;
    const names = inputs.current.tools.filter(tool => tool.available && (
      ['yt-dlp', 'deno', 'qjs'].includes(tool.name) || tool.name === 'ffmpeg' && inputs.current.ffmpegMode === 'efficiency'
    )).map(tool => tool.name);
    const results = await Promise.allSettled(names.map(name => host.command<ToolRelease>('tools.latest', {
      name, ...(name === 'ffmpeg' ? { mode: 'efficiency' } : { channel: 'stable' }),
    }, { signal: controller.signal, timeoutMs: 45000 })));
    running.current = false;
    if (!mounted.current || controller.signal.aborted) return;
    setReleases(results.flatMap(result => result.status === 'fulfilled' ? [result.value] : []));
    setErrors(results.flatMap((result, index) => result.status === 'rejected' ? [`${names[index]}: ${String(result.reason)}`] : []));
    setChecked(true); setBusy('');
  }, []);
  const ready = tools.length > 0;
  useEffect(() => {
    if (!enabled || !ready || started.current) return;
    // Let the task list and local component checks finish before contacting upstream.
    const timer = setTimeout(() => { started.current = true; void check(); }, 3000);
    return () => clearTimeout(timer);
  }, [enabled, ready, check]);
  const available = releases.filter(release => release.updateAvailable &&
    tools.some(tool => tool.name === release.name && tool.version === release.current) &&
    (release.name !== 'ffmpeg' || ffmpegMode === 'efficiency'));
  const install = async (release: ToolRelease) => {
    if (!inputs.current.enabled || running.current || !available.includes(release)) return;
    running.current = true; setBusy(release.name); setErrors(current => current.filter(error => !error.startsWith(`${release.name}:`)));
    try {
      await host.command('tools.update', { name: release.name, version: release.latest,
        ...(release.name === 'ffmpeg' ? { mode: 'efficiency' } : { channel: 'stable' }),
      });
      if (mounted.current) setReleases(current => current.filter(item => item.name !== release.name));
    } catch (reason) { if (mounted.current) setErrors(current => [...current, `${release.name}: ${String(reason)}`]); }
    finally { running.current = false; if (mounted.current) setBusy(''); }
  };
  return { available, errors, busy, checked, check, install };
}

export type RuntimeUpdateController = ReturnType<typeof useRuntimeUpdates>;

export function RuntimeUpdatePanel({ update, disabled, t }: { update: RuntimeUpdateController; disabled: boolean; t: Translate }) {
  return <section className="settings-group panel" aria-label={t('组件上游更新', 'Upstream component updates')}>
    <div className="section-heading"><h2>{t('组件上游更新', 'Upstream component updates')}</h2><Button icon="refresh" disabled={disabled || Boolean(update.busy)} onClick={() => { void update.check(); }}>{update.busy === 'check' ? t('检查中…', 'Checking…') : t('检查上游更新', 'Check upstream updates')}</Button></div>
    <p className="hint">{t('启动后自动检查 yt-dlp 稳定版及当前 JavaScript 运行时，点击安装后才下载。FFmpeg 仅在效率优先时检查上游；兼容优先保持锁定版本。内置抖音插件随程序更新。', 'Startup checks the stable yt-dlp release and current JavaScript runtime; downloads start only after clicking Install. FFmpeg checks upstream in Efficiency mode; Compatibility keeps the pinned build. The bundled Douyin plugin updates with the application.')}</p>
    <p className="hint">{t('更新用于新任务；已有任务需选择“使用当前设置重试”才能使用新组件。', 'Updates apply to new tasks. Use “Retry with current settings” for existing tasks to use new components.')}</p>
    {update.available.map(release => <div className="component-row" key={release.name}><div><strong>{release.name}</strong><p className="muted">{release.current} → {release.latest}</p></div><Button icon="download" disabled={disabled || Boolean(update.busy)} onClick={() => { void update.install(release); }}>{update.busy === release.name ? t('安装中…', 'Installing…') : t(`安装 ${release.name} ${release.latest}`, `Install ${release.name} ${release.latest}`)}</Button></div>)}
    {update.checked && !update.busy && !update.available.length && !update.errors.length && <p className="notice" role="status">{t('已检查的组件没有可用更新。', 'No updates are available for the checked components.')}</p>}
    {update.errors.map(error => <p className="notice danger" role="alert" key={error}><strong>{error.split(':', 1)[0]}</strong>: {explainError(error, t)}</p>)}
  </section>;
}
