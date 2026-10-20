import { useEffect, useRef, useState } from 'react';
import { Button, Empty, Field, Modal } from './components';
import { host } from './transport';
import { explainError } from './errors';
import type { CoverResult, Media } from './types';
import type { Translate } from './i18n';
import type { Run } from './App';

interface Edit { preset: string; fitMode: string; focusX: number; focusY: number; quality: number; backgroundColor: string }
export function CoverStudio({ media, close, run, t }: { media: Media; close: () => void; run: Run; t: Translate }) {
  const [edit, setEdit] = useState<Edit>({ preset: 'landscape_16_9', fitMode: 'crop', focusX: 0.5, focusY: 0.5, quality: 90, backgroundColor: '#000000' });
  const [assetId, setAssetId] = useState(''); const [original, setOriginal] = useState(false); const [preview, setPreview] = useState(''); const [error, setError] = useState('');
  const [prompt, setPrompt] = useState(t('保留原始主体和品牌细节，改善构图、光线、清晰度和点击吸引力，不添加误导性文字或人物。', 'Preserve the subject and brand details. Improve composition, lighting, clarity, and appeal without misleading text or people.'));
  const [operation, setOperation] = useState<'upload' | 'export' | 'generate' | null>(null); const [loading, setLoading] = useState(false); const [dirty, setDirty] = useState(false); const [status, setStatus] = useState('');
  const busy = operation !== null;
  const uploadRef = useRef<HTMLInputElement>(null);
  const uploadId = useRef('');
  const mounted = useRef(true);
  const generationCancelRequested = useRef(false);
  const releaseUpload = (id: string) => { if (id) run(() => host.command('cover.release_upload', { assetId: id })); };
  const replaceAsset = (id: string, uploaded = false) => {
    const previous = uploadId.current;
    uploadId.current = uploaded ? id : '';
    if (previous && previous !== id) releaseUpload(previous);
    setAssetId(id);
  };
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; releaseUpload(uploadId.current); uploadId.current = ''; };
  }, []);
  useEffect(() => {
    let disposed = false; setLoading(true);
    const timer = setTimeout(() => { void host.command<CoverResult & { previewUrl: string }>('cover.preview', { mediaId: media.id, assetId, original, ...edit }).then(result => { if (!disposed) { setPreview(result.previewUrl); setError(''); } }).catch(reason => { if (!disposed) setError(String(reason)); }).finally(() => { if (!disposed) setLoading(false); }); }, 180);
    return () => { disposed = true; clearTimeout(timer); };
  }, [media.id, assetId, original, edit]);
  const change = <K extends keyof Edit>(key: K, value: Edit[K]) => { setEdit(current => ({ ...current, [key]: value })); setDirty(true); };
  const discard = () => !dirty || window.confirm(t('放弃未导出的封面修改？', 'Discard cover changes that have not been exported?'));
  const requestClose = () => {
    if (operation === 'generate') { generationCancelRequested.current = true; run(async () => { try { await host.command('cover.cancel', { mediaId: media.id }); setStatus(t('正在取消 AI 创作…', 'Cancelling AI creation…')); } catch (reason) { generationCancelRequested.current = false; throw reason; } }); return; }
    if (operation === 'export') return;
    if (discard()) close();
  };
  const exportCover = () => { setOperation('export'); setError(''); run(async () => { try { const result = await host.command<CoverResult & { previewUrl: string }>('cover.export', { mediaId: media.id, assetId, original, ...edit }); setPreview(result.previewUrl); setDirty(false); setStatus(t(`已导出 ${result.filename || 'JPEG'}`, `Exported ${result.filename || 'JPEG'}`)); await host.result(media.id, 'download', 'cover'); } catch (reason) { setError(String(reason)); } finally { setOperation(null); } }); };
  const generate = () => { generationCancelRequested.current = false; setOperation('generate'); setError(''); setStatus(t('正在生成 AI 封面…', 'Generating AI cover…')); run(async () => { try { const result = await host.command<CoverResult & { previewUrl: string }>('cover.generate', { mediaId: media.id, assetId, original, ...edit, prompt }); replaceAsset(result.assetId || ''); setOriginal(false); setPreview(result.previewUrl); setDirty(true); setStatus(t('AI 变体已生成，可调整裁切并导出 JPEG。', 'AI variation generated. Adjust the crop and export JPEG.')); } catch (reason) { if (generationCancelRequested.current) setStatus(t('AI 创作已取消', 'AI creation cancelled')); else { setError(String(reason)); setStatus(''); } } finally { setOperation(null); } }); };
  return <Modal title={t('封面工作室', 'Cover studio')} subtitle={media.title} t={t} wide close={requestClose} footer={<><Button icon="refresh" disabled={busy} onClick={() => { if (discard()) { replaceAsset(''); setOriginal(true); setDirty(true); } }}>{t('恢复原始封面', 'Restore original cover')}</Button><Button icon="image" disabled={busy} onClick={() => uploadRef.current?.click()}>{operation === 'upload' ? t('上传中…', 'Uploading…') : t('上传图片', 'Upload image')}</Button><span className="footer-spacer" /><Button disabled={operation === 'export'} onClick={requestClose}>{operation === 'generate' ? t('取消 AI 创作', 'Cancel AI creation') : t('关闭', 'Close')}</Button></>}>
    <input ref={uploadRef} hidden type="file" accept="image/png,image/jpeg,image/webp,image/gif,image/bmp" onChange={event => { const file = event.target.files?.[0]; event.target.value = ''; if (!file || busy) return; setOperation('upload'); run(async () => { try { if (file.size > 25 * 1024 * 1024) throw new Error(t('图片不能超过 25 MiB', 'Image must be at most 25 MiB')); const value = await host.upload(file); if (!mounted.current) { releaseUpload(value.id); return; } replaceAsset(value.id, true); setOriginal(false); setDirty(true); } finally { if (mounted.current) setOperation(null); } }); }} />
    <div className="cover-layout"><div className={`cover-preview ${edit.fitMode === 'crop' ? 'crop' : ''}`} role={edit.fitMode === 'crop' ? 'button' : undefined} tabIndex={edit.fitMode === 'crop' ? 0 : undefined} aria-label={t('封面裁切焦点；方向键微调', 'Cover crop focus; use arrow keys to adjust')}
      onClick={event => { if (edit.fitMode !== 'crop' || busy) return; const bounds = event.currentTarget.getBoundingClientRect(); setEdit(current => ({ ...current, focusX: Math.max(0, Math.min(1, (event.clientX - bounds.left) / bounds.width)), focusY: Math.max(0, Math.min(1, (event.clientY - bounds.top) / bounds.height)) })); setDirty(true); }}
      onKeyDown={event => { if (edit.fitMode !== 'crop' || busy || !['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(event.key)) return; event.preventDefault(); setEdit(current => ({ ...current, focusX: Math.max(0, Math.min(1, current.focusX + (event.key === 'ArrowRight' ? 0.02 : event.key === 'ArrowLeft' ? -0.02 : 0))), focusY: Math.max(0, Math.min(1, current.focusY + (event.key === 'ArrowDown' ? 0.02 : event.key === 'ArrowUp' ? -0.02 : 0))) })); setDirty(true); }}>
      {preview ? <img src={preview} alt={t('封面预览', 'Cover preview')} draggable={false} /> : <Empty title={t('上传图片或等待封面预览', 'Upload an image or wait for cover preview')} icon="image" />}<span className="cover-hint">{loading ? t('更新预览…', 'Updating preview…') : edit.fitMode === 'crop' ? t('单击或使用方向键调整裁切焦点', 'Click or use arrow keys to adjust crop focus') : t('留白模式保留完整图像', 'Padding preserves the full image')}</span>
    </div><div className="cover-controls"><Field label={t('封面预设', 'Cover preset')}><select disabled={busy} value={edit.preset} onChange={event => change('preset', event.target.value)}>{[['landscape_16_9', '横版 16:9', 'Landscape 16:9'], ['portrait_3_4', '竖版 3:4', 'Portrait 3:4'], ['portrait_9_16', '竖版 9:16', 'Portrait 9:16'], ['landscape_4_3', '横版 4:3', 'Landscape 4:3'], ['square_1_1', '正方形 1:1', 'Square 1:1']].map(([value, zh, en]) => <option key={value} value={value}>{t(zh, en)}</option>)}</select></Field>
      <Field label={t('适配方式', 'Fit mode')}><select value={edit.fitMode} disabled={busy} onChange={event => change('fitMode', event.target.value)}><option value="crop">{t('裁切填满', 'Crop to fill')}</option><option value="pad">{t('全图留白', 'Fit with padding')}</option></select></Field>
      {(['focusX', 'focusY'] as const).map((key, index) => <Field key={key} label={`${t(index ? '裁切焦点 Y' : '裁切焦点 X', index ? 'Crop focus Y' : 'Crop focus X')} · ${Math.round(edit[key] * 100)}%`}><input type="range" min={0} max={100} value={Math.round(edit[key] * 100)} disabled={busy || edit.fitMode !== 'crop'} onChange={event => change(key, Number(event.target.value) / 100)} /></Field>)}
      <Field label={t('JPEG 质量', 'JPEG quality')}><input type="number" min={1} max={100} value={edit.quality} disabled={busy} onChange={event => change('quality', Number(event.target.value))} /></Field>{edit.fitMode === 'pad' && <Field label={t('留白颜色', 'Padding color')}><input type="color" value={edit.backgroundColor} disabled={busy} onChange={event => change('backgroundColor', event.target.value)} /></Field>}
      <Button tone="primary" icon="download" disabled={busy || !preview || loading} onClick={exportCover}>{operation === 'export' ? t('导出中…', 'Exporting…') : t('导出 JPEG', 'Export JPEG')}</Button><hr /><Field label={t('AI 封面提示词', 'AI cover prompt')}><textarea rows={4} value={prompt} maxLength={8000} disabled={busy} onChange={event => setPrompt(event.target.value)} /></Field><Button icon="image" disabled={busy || !prompt.trim() || !preview} onClick={generate}>{operation === 'generate' ? t('生成中…', 'Generating…') : t('生成 AI 变体', 'Generate AI variation')}</Button>
    </div></div>{error && <p className="notice danger" role="alert">{explainError(error, t)}</p>}{status && <p className="notice" role="status">{status}</p>}
  </Modal>;
}
