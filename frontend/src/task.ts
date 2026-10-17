import type { MediaInfo, Task } from './types';
import type { Translate } from './i18n';

export function taskActions(task: Task, t: Translate): { action: string; label: string; icon: string }[] {
  const actions = [];
  if (task.status === 'paused') actions.push({ action: 'resume', label: t('继续', 'Resume'), icon: 'play' });
  else if (['failed', 'processing_failed', 'cancelled'].includes(task.status)) actions.push({ action: 'retry', label: task.status === 'processing_failed' ? t('重试处理', 'Retry processing') : t('重试下载', 'Retry download'), icon: 'refresh' });
  else if (['queued', 'probing', 'downloading', 'processing', 'waiting_processing'].includes(task.status)) actions.push({ action: 'pause', label: t('暂停', 'Pause'), icon: 'pause' });
  if (['queued', 'probing', 'downloading', 'processing', 'waiting_processing', 'paused'].includes(task.status)) actions.push({ action: 'cancel', label: t('取消', 'Cancel'), icon: 'close' });
  return actions;
}

export function taskProgress(task: Task): { value: number; indeterminate: boolean } {
  const processing = ['processing', 'waiting_processing', 'processing_failed'].includes(task.status);
  const frozen = ['paused', 'completed', 'failed', 'cancelled', 'processing_failed'].includes(task.status);
  const raw = task.status === 'completed' ? 100 : processing ? task.stageProgress : task.stageProgress ?? task.progress;
  return { value: Math.max(0, Math.min(100, Number(raw) || 0)), indeterminate: !frozen && (task.stageIndeterminate || processing && task.stageProgress === null) };
}
export function statusLabel(status: string, t: Translate): string {
  const labels: Record<string, [string, string]> = { queued: ['下载排队', 'Download queued'], probing: ['解析媒体', 'Reading media'], downloading: ['下载中', 'Downloading'], waiting_processing: ['等待处理', 'Processing queued'], processing: ['处理中', 'Processing'], processing_failed: ['处理失败', 'Processing failed'], paused: ['已暂停', 'Paused'], cancelled: ['已取消', 'Cancelled'], failed: ['下载失败', 'Download failed'], completed: ['已完成', 'Completed'] };
  return labels[status] ? t(...labels[status]) : status;
}
export function statusTone(status: string): string { return status.includes('failed') ? 'danger' : status === 'completed' ? 'success' : ['downloading', 'processing'].includes(status) ? 'accent' : ''; }
export function stageLabel(stage: string, status: string, t: Translate): string {
  if (stage === 'probing') return t('正在确定工序', 'Determining workflow');
  if (stage.startsWith('downloading_video_')) return t('保存视频画面', 'Saving video stream');
  if (stage.startsWith('downloading_audio_')) return t('保存音轨', 'Saving audio track');
  if (stage.startsWith('converting_subtitle_')) return t('转换字幕文件', 'Converting subtitles');
  if (stage.startsWith('splitting_chapter_')) return t('拆分章节文件', 'Splitting chapters');
  const labels: Record<string, [string, string]> = { probe: ['读取媒体信息', 'Reading media information'], metadata: ['读取媒体信息', 'Reading media information'], download: ['保存来源媒体', 'Saving source media'], downloading_assets: ['保存封面与附加文件', 'Saving cover and additional files'], verifying_sources: ['验证来源文件', 'Validating source files'], muxing: ['合并视频与音轨', 'Merging video and audio'], encoding: ['转换视频编码', 'Converting video codec'], merge: ['合并视频与音轨', 'Merging video and audio'], transcode: ['转换视频编码', 'Converting video codec'], subtitles: ['处理字幕', 'Preparing subtitles'], thumbnail: ['准备封面', 'Preparing cover'], cover: ['准备封面', 'Preparing cover'], prepend_cover: ['添加封面片头', 'Adding cover intro'], validate: ['验证结果文件', 'Validating result file'], verifying_output: ['验证结果文件', 'Validating result file'], validating_publication: ['验证已保存结果', 'Validating saved result'], publishing: ['保存结果文件', 'Saving result file'], publish: ['保存结果文件', 'Saving result file'], publication: ['保存结果文件', 'Saving result file'] };
  return labels[stage] ? t(...labels[stage]) : statusLabel(stage in { queued: 1, probing: 1, downloading: 1, waiting_processing: 1, processing: 1, processing_failed: 1, paused: 1, cancelled: 1, failed: 1, completed: 1 } ? stage : status, t);
}
export function stepLabel(task: Task): string { return task.stepIndex && task.stepCount ? `${task.stepIndex}/${task.stepCount} ` : ''; }
export function dimensions(info?: MediaInfo | null): string {
  return info?.width && info.height ? `${info.width} × ${info.height}` : '—';
}
export function mediaQuality(info?: MediaInfo | null): string {
  if (!info?.width || !info.height) return '—';
  const shorter = Math.min(info.width, info.height), longer = Math.max(info.width, info.height);
  if (longer >= 7680 && shorter >= 3200) return '8K';
  if (longer >= 3840 && shorter >= 1600) return '4K';
  for (const height of [2160, 1440, 1080, 720, 480, 360, 240, 144]) if (shorter >= height) return `${height}p`;
  return `${shorter}p`;
}
export function frameRate(value?: number | null): string { return value && Number.isFinite(value) ? `${Number(value.toFixed(2))} fps` : '—'; }
export function bytes(value: number | string | undefined | null): string {
  if (typeof value === 'string') return value || '—';
  if (!value) return '—';
  const power = Math.min(4, Math.floor(Math.log(value) / Math.log(1024)));
  return `${(value / 1024 ** power).toFixed(power ? 1 : 0)} ${['B', 'KiB', 'MiB', 'GiB', 'TiB'][power]}`;
}
export function duration(value: number | string | undefined | null): string {
  if (typeof value === 'string') return value || '—';
  if (value == null || !Number.isFinite(value)) return '—';
  const seconds = Math.max(0, Math.round(value));
  return seconds >= 3600 ? `${Math.floor(seconds / 3600)}:${String(Math.floor(seconds / 60) % 60).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}` : `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
}
