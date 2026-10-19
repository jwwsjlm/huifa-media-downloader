import { useEffect, useMemo, useRef, useState } from 'react';
import { Button, Check, Field, Modal } from './components';
import { host } from './transport';
import { explainError } from './errors';
import { newRequestId } from './requestId';
import { compressionReason, estimateSize, formatBitrate, formatSize, frameRateChoices, parseSize, parseVideoBitrate, resolutionChoices, sizeInput, type CompressionOptions, type CompressionPreview } from './compression';
import type { Run } from './App';
import type { Translate } from './i18n';
import type { Task } from './types';
import './CompressionDialog.css';

export function CompressionDialog({ task, close, queued, run, t }: { task: Task; close: () => void; queued?: (task: Task) => void; run: Run; t: Translate }) {
  const [initial, setInitial] = useState<CompressionPreview | null>(null);
  const [mode, setMode] = useState<'simple' | 'advanced'>('simple');
  const [sizeText, setSizeText] = useState('');
  const [advanced, setAdvanced] = useState({ bitrate: '', resolution: 'original', fps: 'original', encoder: 'libx265' });
  const [preview, setPreview] = useState<{ key: string; value: CompressionPreview } | null>(null);
  const [checking, setChecking] = useState(false); const [submitting, setSubmitting] = useState(false);
  const [cancelRequested, setCancelRequested] = useState(false);
  const preparation = useRef<{ requestId: string; cancelled: boolean } | null>(null);
  const mounted = useRef(false);
  const [error, setError] = useState('');
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      const pending = preparation.current;
      if (pending && !pending.cancelled) { pending.cancelled = true; void host.command('probe.cancel', { id: pending.requestId }).catch(() => {}); }
    };
  }, []);
  useEffect(() => {
    let active = true; const requestId = newRequestId();
    void host.command<CompressionPreview>('tasks.convertPreview', { id: task.id, compression: {}, requestId }).then(value => {
      if (!active) return;
      setInitial(value); setSizeText(sizeInput(value.source.sizeBytes));
    }).catch(reason => { if (active) setError(String(reason)); });
    return () => { active = false; void host.command('probe.cancel', { id: requestId }).catch(() => {}); };
  }, [task.id]);
  const source = initial?.source;
  const resolutions = useMemo(() => source ? resolutionChoices(source.displayWidth, source.displayHeight) : [], [source]);
  const frameRates = useMemo(() => frameRateChoices(source?.fps || null), [source?.fps]);
  const targetBytes = parseSize(sizeText); const videoBitrateBps = parseVideoBitrate(advanced.bitrate);
  const invalid = source ? mode === 'simple'
    ? targetBytes === null ? t('请输入有效大小，例如 4MB、4GB 或 4G；纯数字按 MB。', 'Enter a valid size, such as 4MB, 4GB or 4G. Numbers alone mean MB.')
      : targetBytes > source.sizeBytes ? t('目标大小不能超过原文件实际大小。', 'The target size cannot exceed the actual source file size.') : ''
    : videoBitrateBps === null ? t('请输入有效视频码率，例如 8Mbps 或 800Kbps；纯数字按 Mbps。', 'Enter a valid video bitrate, such as 8Mbps or 800Kbps. Numbers alone mean Mbps.') : '' : '';
  const compression = useMemo<CompressionOptions | null>(() => {
    if (!source || invalid) return null;
    if (mode === 'simple') return { targetBytes: targetBytes!, encoder: advanced.encoder };
    const resolution = resolutions.find(choice => choice.value === advanced.resolution);
    return { videoBitrateBps: videoBitrateBps!, encoder: advanced.encoder,
      ...(resolution && resolution.value !== 'original' ? { maxWidth: resolution.width, maxHeight: resolution.height } : {}),
      ...(advanced.fps !== 'original' ? { fps: advanced.fps } : {}) };
  }, [source, invalid, mode, targetBytes, videoBitrateBps, advanced, resolutions]);
  const key = compression ? JSON.stringify(compression) : '';
  useEffect(() => {
    if (!key) return;
    let active = true; const requestId = newRequestId(); setChecking(true); setError('');
    const timer = window.setTimeout(() => {
      void host.command<CompressionPreview>('tasks.convertPreview', { id: task.id, compression: JSON.parse(key), requestId }).then(value => {
        if (active) setPreview({ key, value });
      }).catch(reason => { if (active) setError(String(reason)); }).finally(() => { if (active) setChecking(false); });
    }, 250);
    return () => { active = false; window.clearTimeout(timer); void host.command('probe.cancel', { id: requestId }).catch(() => {}); };
  }, [task.id, key]);
  const current = preview?.key === key ? preview.value : null;
  const estimatedBytes = current?.plan?.estimatedSizeBytes ?? (source && mode === 'advanced' && videoBitrateBps && preview?.value.plan ? estimateSize(videoBitrateBps, source.duration, preview.value.plan) : null);
  const capabilities = current?.capabilities || initial?.capabilities || [];
  const hardware = capabilities.filter(capability => !['libx264', 'libx265'].includes(capability.encoder));
  const availableGpu = hardware.find(capability => capability.available);
  const selectedGpu = hardware.find(capability => capability.encoder === advanced.encoder);
  const gpu = advanced.encoder !== 'libx265';
  const gpuReason = selectedGpu?.reason || (!gpu && hardware.find(capability => capability.reason)?.reason) || t('没有可用于此视频的 GPU 编码器。', 'No GPU encoder is available for this video.');
  const blocking = current?.blockingReasons || initial?.blockingReasons || [];
  const canStart = Boolean(compression && current?.plan && (!gpu || selectedGpu?.available) && !checking && !submitting && !blocking.length && !error);
  const switchMode = (next: 'simple' | 'advanced') => {
    if (next === 'advanced' && !advanced.bitrate) {
      const bitrate = current?.plan?.videoBitrateBps || source?.videoBitrateBps;
      if (bitrate) setAdvanced(value => ({ ...value, bitrate: formatBitrate(bitrate) }));
    }
    setMode(next);
  };
  const submit = () => {
    if (!canStart || !compression || preparation.current) return;
    const pending = { requestId: newRequestId(), cancelled: false }; preparation.current = pending;
    setSubmitting(true); setCancelRequested(false); setError('');
    run(async () => {
      try { const result = await host.command<Task>('tasks.convert', { id: task.id, compression, requestId: pending.requestId }); queued?.(result); if (mounted.current) close(); }
      catch (reason) { if (mounted.current) { if (pending.cancelled) close(); else setError(String(reason)); } }
      finally { if (preparation.current === pending) preparation.current = null; if (mounted.current) { setSubmitting(false); setCancelRequested(false); } }
    });
  };
  const dismiss = () => {
    const pending = preparation.current;
    if (!pending) { close(); return; }
    if (pending.cancelled) return;
    pending.cancelled = true; setCancelRequested(true);
    void host.command('probe.cancel', { id: pending.requestId }).catch(() => {});
  };
  return <Modal title={t('压缩视频', 'Compress video')} subtitle={task.title} t={t} close={dismiss} footer={<><Button disabled={cancelRequested} onClick={dismiss}>{cancelRequested ? t('取消准备…', 'Cancelling preparation…') : submitting ? t('取消准备', 'Cancel preparation') : t('取消', 'Cancel')}</Button><Button tone="primary" disabled={!canStart} onClick={submit}>{submitting ? t('加入队列中…', 'Adding to queue…') : t('开始压缩', 'Start compression')}</Button></>}>
    <div className="compression-dialog">
      {!source ? <p className="hint" role="status">{t('正在检查本地视频与编码器…', 'Checking the local video and encoders…')}</p> : <>
        <dl className="compression-source"><div><dt>{t('原文件', 'Source file')}</dt><dd>{formatSize(source.sizeBytes)}</dd></div><div><dt>{t('分辨率', 'Resolution')}</dt><dd>{source.displayWidth} × {source.displayHeight}</dd></div><div><dt>{t('当前视频码率', 'Current video bitrate')}</dt><dd>{source.videoBitrateBps ? `${source.videoBitrateEstimated ? '≈ ' : ''}${formatBitrate(source.videoBitrateBps)}` : t('无法确定', 'Unavailable')}</dd></div></dl>
        <div className="filter-tabs" aria-label={t('压缩模式', 'Compression mode')}><Button disabled={submitting} aria-pressed={mode === 'simple'} tone={mode === 'simple' ? 'primary' : ''} onClick={() => switchMode('simple')}>{t('简单', 'Simple')}</Button><Button disabled={submitting} aria-pressed={mode === 'advanced'} tone={mode === 'advanced' ? 'primary' : ''} onClick={() => switchMode('advanced')}>{t('高级', 'Advanced')}</Button></div>
        {mode === 'simple' ? <>
          <Field label={t('目标大小', 'Target size')} hint={t('支持 4MB、4GB、4G 和 MiB / GiB；MB / GB 为十进制。', 'Accepts 4MB, 4GB, 4G and MiB / GiB. MB / GB use decimal units.')}><input aria-label={t('目标大小', 'Target size')} value={sizeText} maxLength={48} aria-invalid={Boolean(invalid)} disabled={submitting} onChange={event => setSizeText(event.target.value)} /></Field>
          <label className="compression-slider"><span>{t('拖动调整目标大小', 'Drag to set the target size')}</span><input type="range" aria-label={t('目标大小百分比', 'Target size percentage')} min="0" max="100" step="0.1" value={targetBytes ? Math.min(100, targetBytes / source.sizeBytes * 100) : 0} aria-valuetext={`${targetBytes ? formatSize(targetBytes) : '—'} / ${formatSize(source.sizeBytes)}`} disabled={submitting} onChange={event => setSizeText(sizeInput(Math.max(1, Math.floor(source.sizeBytes * Number(event.target.value) / 100))))} /><span className="compression-scale"><small>0 MB</small><small>{formatSize(source.sizeBytes)}</small></span></label>
          <p className="hint">{t('保留原分辨率和帧率；目标越小，画面损失越明显。', 'Keeps the source resolution and frame rate. Smaller targets reduce picture quality.')}</p>
          {!invalid && targetBytes && <strong>{t(`目标减少 ${Number(((1 - targetBytes / source.sizeBytes) * 100).toFixed(1))}%`, `Target reduction: ${Number(((1 - targetBytes / source.sizeBytes) * 100).toFixed(1))}%`)}</strong>}
        </> : <>
          <Field label={t('视频码率', 'Video bitrate')} hint={t('码率决定输出，文件大小仅估算，不作为上限。', 'Bitrate controls the output; file size is estimated only, without a size limit.')}><input aria-label={t('视频码率', 'Video bitrate')} value={advanced.bitrate} maxLength={48} aria-invalid={Boolean(invalid)} disabled={submitting} onChange={event => setAdvanced(value => ({ ...value, bitrate: event.target.value }))} /></Field>
          <div className="form-grid"><Field label={t('分辨率', 'Resolution')}><select aria-label={t('分辨率', 'Resolution')} value={advanced.resolution} disabled={submitting} onChange={event => setAdvanced(value => ({ ...value, resolution: event.target.value }))}>{resolutions.map(choice => <option key={choice.value} value={choice.value}>{choice.value === 'original' ? `${t('保持原始', 'Keep source')} · ${choice.label}` : choice.label}</option>)}</select></Field><Field label={t('帧率', 'Frame rate')}><select aria-label={t('帧率', 'Frame rate')} value={advanced.fps} disabled={submitting} onChange={event => setAdvanced(value => ({ ...value, fps: event.target.value }))}>{frameRates.map(choice => <option key={choice.value} value={choice.value}>{choice.value === 'original' ? `${t('保持原始', 'Keep source')} · ${source.variableFrameRate ? `${t('可变帧率', 'Variable frame rate')} · ${choice.label}` : choice.label}` : choice.label}</option>)}</select></Field></div>
          {source.variableFrameRate && advanced.fps !== 'original' && <p className="notice">{t('选择固定帧率会改变原视频的可变帧率节奏。', 'Selecting a fixed rate changes the source’s variable frame timing.')}</p>}
        </>}
        <Check label={t('GPU 编码加速', 'GPU encoding acceleration')} checked={gpu} disabled={submitting || !gpu && !availableGpu} onChange={event => setAdvanced(value => ({ ...value, encoder: event.target.checked && availableGpu ? availableGpu.encoder : 'libx265' }))} />
        <p className="hint">{gpu ? `${selectedGpu?.label || advanced.encoder} · ${selectedGpu?.available ? t('编码失败时会停止，可选择 CPU 重试。', 'Encoding failures stop the task; CPU retry remains available.') : compressionReason(gpuReason, t)}` : availableGpu?.label || compressionReason(gpuReason, t)}</p>
        {invalid && <p className="notice danger" role="alert">{invalid}</p>}
        {!invalid && (mode === 'simple' && current?.plan || mode === 'advanced' && estimatedBytes) && <div className="compression-estimate" role="status"><strong>{mode === 'simple' ? t('目标上限', 'Target limit') : t('估算输出大小', 'Estimated output size')} · {formatSize(mode === 'simple' ? targetBytes! : estimatedBytes!)}</strong>{current?.plan && <span>{current.plan.width} × {current.plan.height} · {formatBitrate(current.plan.videoBitrateBps)}</span>}</div>}
        {checking && !invalid && <p className="hint" role="status">{t('正在更新压缩预览…', 'Updating compression preview…')}</p>}
        {blocking.map(reason => <p key={reason} className="notice danger" role="alert">{compressionReason(reason, t)}</p>)}
        {current?.warnings.map(warning => <p key={warning} className="notice">{compressionReason(warning, t)}</p>)}
        <p className="hint">{t('输出：', 'Output: ')}HEVC · MKV · {source.hdr.toUpperCase()} · {source.bitDepth}-bit · {t(`保留 ${source.audioTracks} 条音轨、${source.subtitleTracks} 条字幕和 ${source.attachmentTracks} 个附件。另存新文件，原文件保留。`, `Preserves ${source.audioTracks} audio tracks, ${source.subtitleTracks} subtitle tracks and ${source.attachmentTracks} attachments. Saves a new file and keeps the source.`)}</p>
      </>}
      {error && <p className="notice danger" role="alert">{explainError(error, t)}</p>}
    </div>
  </Modal>;
}
