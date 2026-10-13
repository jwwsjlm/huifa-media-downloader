import { memo } from 'react';
import { Check, Field } from './components';
import type { DownloadOptions, OptionValue } from './types';
import type { Translate } from './i18n';

export interface OptionField { key: string; zh: string; en: string; type?: 'number' | 'check' | 'date'; choices?: [string, string, string][]; min?: number; max?: number; hint?: [string, string] }
const choices = (values: string[]) => values.map(value => [value, value, value] as [string, string, string]);
export const encoders = ['original', 'libx264', 'libx265', 'libsvtav1', 'libaom-av1', 'librav1e', 'h264_nvenc', 'hevc_nvenc', 'av1_nvenc', 'h264_qsv', 'hevc_qsv', 'av1_qsv', 'h264_amf', 'hevc_amf', 'av1_amf'];
export const defaults: DownloadOptions = {
  contentMode: 'video', quality: 'best', audioFormat: 'best', audioTrack: 'default', container: 'auto', videoFps: 'best', sourceVideoCodec: 'auto', vrMode: 'any', compatibilityTarget: 'auto',
  collectionMode: 'select', collectionOrder: 'original', firstN: 0, playlistItems: '', dateAfter: '', dateBefore: '', durationMin: 0, durationMax: 0,
  liveFilter: 'videos', liveFromStart: false, waitForLive: false, waitMin: 30, waitMax: 120, sectionStart: '', sectionEnd: '', splitChapters: false,
  subtitleLanguage: 'none', subtitleFormat: 'best', automaticSubtitles: true, embedSubtitles: false, writeThumbnail: true, writeDescription: false, writeComments: false, writeInfoJson: false, embedMetadata: false, embedChapters: false, embedThumbnail: false,
  sponsorblockMode: 'off', sponsorblockCategories: ['sponsor'], rateLimit: '', encoder: 'original', organizeTaskFolder: true, filenameTemplate: '%(title)s [%(id)s].%(ext)s',
  convertCoverJpeg: true, coverJpegQuality: 90, prependCoverEnabled: false, prependCoverFrames: 3, fragmentConcurrent: 1, requestDelay: 0,
};
export function commandOptions(options: DownloadOptions): DownloadOptions {
  const { proxyConfigured: _proxyConfigured, ...value } = options;
  return value;
}
export const optionGroups: { key: string; title: [string, string]; fields: OptionField[] }[] = [
  { key: 'media', title: ['媒体格式与处理', 'Media format and processing'], fields: [
    { key: 'contentMode', zh: '内容类型', en: 'Content type', choices: [['video', '视频与音频', 'Video with audio'], ['audio', '仅音频', 'Audio only'], ['manual', '逐任务选择格式', 'Choose format per task']] },
    { key: 'quality', zh: '清晰度上限', en: 'Quality limit', choices: [['best', '最佳可用画质', 'Best available quality'], ...choices(['8k', '4k', '2k', '1080p', '720p', '480p', '360p', '240p']), ['qcif', '144p', '144p']] },
    { key: 'audioFormat', zh: '音频输出格式', en: 'Audio output format', choices: choices(['best', 'aac', 'alac', 'flac', 'm4a', 'mp3', 'opus', 'vorbis', 'wav']) },
    { key: 'audioTrack', zh: '音轨偏好', en: 'Audio track preference', choices: [['default', '默认音轨', 'Default audio'], ['original', '原始音轨', 'Original audio'], ['all', '全部音轨', 'All tracks'], ...choices(['zh-Hans', 'zh-Hant', 'en', 'ja', 'ko', 'es', 'fr', 'de', 'ru', 'pt', 'ar', 'hi', 'id', 'vi', 'th', 'tr'])] },
    { key: 'container', zh: '视频容器', en: 'Video container', choices: [['auto', '自动 / 来源格式', 'Automatic / source format'], ...choices(['mp4', 'mkv'])] },
    { key: 'videoFps', zh: '帧率偏好', en: 'Preferred frame rate', choices: choices(['best', '240', '120', '60', '50', '30', '25', '24']) },
    { key: 'sourceVideoCodec', zh: '来源编码偏好', en: 'Preferred source codec', choices: choices(['auto', 'h264', 'h265', 'av1', 'vp9']) },
    { key: 'vrMode', zh: 'VR 格式偏好', en: 'VR format preference', choices: [['any', '任意', 'Any'], ['none', '普通平面视频', 'Standard video'], ...choices(['2d360', '3d180', '3d360'])] },
    { key: 'compatibilityTarget', zh: '兼容设备', en: 'Device compatibility', choices: choices(['auto', 'windows', 'macos', 'linux', 'ios', 'android']) },
    { key: 'encoder', zh: '下载后视频编码器', en: 'Video encoder after download', choices: [['original', '保持原编码', 'Keep source codec'], ...choices(encoders.slice(1))], hint: ['硬件编码器需要兼容设备', 'Hardware encoders require compatible devices'] },
    { key: 'organizeTaskFolder', zh: '为每个任务创建文件夹', en: 'Create a folder for each task', type: 'check' },
    { key: 'filenameTemplate', zh: '文件名模板', en: 'Filename template', hint: ['yt-dlp 模板，例如 %(title)s [%(id)s].%(ext)s', 'yt-dlp template, e.g. %(title)s [%(id)s].%(ext)s'] },
  ] },
  { key: 'collection', title: ['合集与筛选', 'Collections and filters'], fields: [
    { key: 'collectionMode', zh: '合集处理', en: 'Collection processing', choices: [['select', '弹出项目选择', 'Choose items'], ['all', '下载全部', 'Download all'], ['single', '仅单个视频', 'Single video only']] },
    { key: 'collectionOrder', zh: '下载顺序', en: 'Download order', choices: [['original', '来源顺序', 'Source order'], ['reverse', '反向顺序', 'Reverse order'], ['random', '随机顺序', 'Random order']] },
    { key: 'firstN', zh: '仅前 N 项（0 不限制）', en: 'First N items (0 for no limit)', type: 'number', min: 0, max: 1000000 },
    { key: 'playlistItems', zh: '指定项目编号', en: 'Specific item indices', hint: ['例如 1,3,5-10', 'For example 1,3,5-10'] },
    { key: 'dateAfter', zh: '上传日期起始', en: 'Uploaded after', type: 'date' }, { key: 'dateBefore', zh: '上传日期结束', en: 'Uploaded before', type: 'date' },
    { key: 'durationMin', zh: '最小时长（秒）', en: 'Minimum duration (seconds)', type: 'number', min: 0, max: 31536000 }, { key: 'durationMax', zh: '最大时长（秒）', en: 'Maximum duration (seconds)', type: 'number', min: 0, max: 31536000 },
  ] },
  { key: 'live', title: ['直播、片段与章节', 'Live streams, sections, and chapters'], fields: [
    { key: 'liveFilter', zh: '直播筛选', en: 'Live stream filter', choices: [['videos', '仅非直播视频', 'Non-live videos'], ['all', '全部内容', 'All content'], ['live', '仅直播', 'Live streams only']] },
    { key: 'liveFromStart', zh: '从直播开始处下载', en: 'Download live streams from the beginning', type: 'check' }, { key: 'waitForLive', zh: '等待尚未开始的直播', en: 'Wait for upcoming live streams', type: 'check' },
    { key: 'waitMin', zh: '最短轮询间隔（秒）', en: 'Minimum poll interval (seconds)', type: 'number', min: 1, max: 86400 }, { key: 'waitMax', zh: '最长轮询间隔（秒）', en: 'Maximum poll interval (seconds)', type: 'number', min: 1, max: 86400 },
    { key: 'sectionStart', zh: '片段开始时间', en: 'Section start time', hint: ['秒数或 HH:MM:SS', 'Seconds or HH:MM:SS'] }, { key: 'sectionEnd', zh: '片段结束时间', en: 'Section end time', hint: ['秒数或 HH:MM:SS', 'Seconds or HH:MM:SS'] },
    { key: 'splitChapters', zh: '按章节拆分文件', en: 'Split files by chapters', type: 'check' },
  ] },
  { key: 'extras', title: ['字幕、封面与元数据', 'Subtitles, covers, and metadata'], fields: [
    { key: 'subtitleLanguage', zh: '字幕语言', en: 'Subtitle language', hint: ['none 不下载；例如 zh-Hans、en、all', 'none disables subtitles; e.g. zh-Hans, en, all'] },
    { key: 'subtitleFormat', zh: '字幕文件格式', en: 'Subtitle file format', choices: choices(['best', 'vtt', 'srt', 'ass', 'lrc']) },
    ...[['automaticSubtitles', '包含自动生成字幕', 'Include automatically generated subtitles'], ['embedSubtitles', '嵌入字幕', 'Embed subtitles'], ['writeThumbnail', '保存来源封面', 'Save source cover'], ['writeDescription', '保存描述文件', 'Save description file'], ['writeComments', '保存评论', 'Save comments'], ['writeInfoJson', '保存媒体信息文件', 'Save media information file'], ['embedMetadata', '嵌入标题和描述', 'Embed title and description'], ['embedChapters', '嵌入章节', 'Embed chapters'], ['embedThumbnail', '嵌入封面', 'Embed cover'], ['convertCoverJpeg', '下载封面自动转为 JPEG', 'Convert downloaded covers to JPEG'], ['prependCoverEnabled', '在视频片头插入下载封面', 'Prepend the downloaded cover to video']].map(([key, zh, en]) => ({ key, zh, en, type: 'check' as const })),
    { key: 'coverJpegQuality', zh: 'JPEG 质量', en: 'JPEG quality', type: 'number', min: 50, max: 100 }, { key: 'prependCoverFrames', zh: '片头封面帧数', en: 'Cover intro frame count', type: 'number', min: 1, max: 300 },
  ] },
  { key: 'sponsor', title: ['SponsorBlock 与速度限制', 'SponsorBlock and speed limit'], fields: [
    { key: 'sponsorblockMode', zh: 'SponsorBlock 行为', en: 'SponsorBlock mode', choices: [['off', '关闭', 'Off'], ['mark', '标记片段', 'Mark segments'], ['remove', '移除片段', 'Remove segments']] },
    { key: 'rateLimit', zh: '下载速度上限', en: 'Download speed limit', hint: ['留空不限速；例如 5M、800K', 'Leave empty for no limit; e.g. 5M, 800K'] },
  ] },
];
export const OptionsForm = memo(function OptionsForm({ options, change, t, quick = false }: { options: DownloadOptions; change: (key: string, value: OptionValue) => void; t: Translate; quick?: boolean }) {
  return <>{optionGroups.map(group => <details key={group.key} className="settings-fold panel" open={quick || group.key === 'media'}><summary><strong>{t(...group.title)}</strong></summary><div className="form-grid">
    {group.fields.filter(field => !quick || ['contentMode', 'quality', 'audioFormat', 'container', 'encoder', 'collectionMode', 'subtitleLanguage', 'prependCoverEnabled'].includes(field.key)).map(field => <OptionControl key={field.key} field={field} value={options[field.key] ?? defaults[field.key]} change={change} t={t} />)}
  </div>{group.key === 'sponsor' && !quick && <div className="checkbox-grid">{['sponsor', 'intro', 'outro', 'selfpromo', 'interaction', 'preview', 'music_offtopic', 'filler'].map(category => <Check key={category} label={category} checked={Array.isArray(options.sponsorblockCategories) && options.sponsorblockCategories.includes(category)} onChange={event => { const values = new Set(Array.isArray(options.sponsorblockCategories) ? options.sponsorblockCategories : []); event.target.checked ? values.add(category) : values.delete(category); change('sponsorblockCategories', [...values]); }} />)}</div>}</details>)}</>;
});
function OptionControl({ field, value, change, t }: { field: OptionField; value: OptionValue | undefined; change: (key: string, value: OptionValue) => void; t: Translate }) {
  const label = t(field.zh, field.en);
  if (field.type === 'check') return <Check label={label} checked={value === true} onChange={event => change(field.key, event.target.checked)} />;
  return <Field label={label} hint={field.hint && t(...field.hint)}>{field.choices ? <select value={String(value ?? '')} onChange={event => change(field.key, event.target.value)}>{field.choices.map(([id, zh, en]) => <option key={id} value={id}>{t(zh, en)}</option>)}</select> : <input type={field.type || 'text'} value={String(value ?? '')} min={field.min} max={field.max} onChange={event => change(field.key, field.type === 'number' ? Number(event.target.value) : event.target.value)} />}</Field>;
}
