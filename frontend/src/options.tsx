import { memo } from 'react';
import { Check, Field } from './components';
import type { DownloadOptions, OptionValue } from './types';
import type { Translate } from './i18n';

export interface OptionField { key: string; zh: string; en: string; type?: 'number' | 'check' | 'date' | 'boolean'; choices?: [string, string, string][]; min?: number; max?: number; hint?: [string, string] }
const choiceNames: Record<string, [string, string]> = {
  best: ['最佳匹配', 'Best match'], auto: ['自动', 'Automatic'], original: ['保持原编码', 'Keep source codec'], copy: ['保持原编码', 'Keep source codec'], none: ['无字幕', 'No subtitles'],
  libx264: ['H.264（软件）','H.264 (software)'], libx265: ['H.265（软件）','H.265 (software)'], 'libvpx-vp9': ['VP9（软件）','VP9 (software)'], libsvtav1: ['AV1（SVT 软件）','AV1 (SVT software)'], 'libaom-av1': ['AV1（AOM 软件）','AV1 (AOM software)'], librav1e: ['AV1（rav1e 软件）','AV1 (rav1e software)'],
  h264_nvenc: ['H.264（NVIDIA）','H.264 (NVIDIA)'], hevc_nvenc: ['H.265（NVIDIA）','H.265 (NVIDIA)'], av1_nvenc: ['AV1（NVIDIA）','AV1 (NVIDIA)'], h264_qsv: ['H.264（Intel）','H.264 (Intel)'], hevc_qsv: ['H.265（Intel）','H.265 (Intel)'], av1_qsv: ['AV1（Intel）','AV1 (Intel)'], h264_amf: ['H.264（AMD）','H.264 (AMD)'], hevc_amf: ['H.265（AMD）','H.265 (AMD)'], av1_amf: ['AV1（AMD）','AV1 (AMD)'],
  windows: ['Windows', 'Windows'], macos: ['macOS', 'macOS'], linux: ['Linux', 'Linux'], ios: ['iOS', 'iOS'], android: ['Android', 'Android'],
  'zh-Hans': ['简体中文', 'Simplified Chinese'], 'zh-Hant': ['繁體中文', 'Traditional Chinese'], en: ['英语', 'English'], ja: ['日语', 'Japanese'], ko: ['韩语', 'Korean'], es: ['西班牙语', 'Spanish'], fr: ['法语', 'French'], de: ['德语', 'German'], ru: ['俄语', 'Russian'], pt: ['葡萄牙语', 'Portuguese'], ar: ['阿拉伯语', 'Arabic'], hi: ['印地语', 'Hindi'], id: ['印尼语', 'Indonesian'], vi: ['越南语', 'Vietnamese'], th: ['泰语', 'Thai'], tr: ['土耳其语', 'Turkish'],
  sponsor: ['赞助广告', 'Sponsors'], intro: ['开场', 'Intros'], outro: ['片尾', 'Outros'], selfpromo: ['自我推广', 'Self-promotion'], interaction: ['互动提醒', 'Interaction reminders'], preview: ['预告', 'Previews'], music_offtopic: ['非音乐片段', 'Non-music segments'], filler: ['冗余片段', 'Filler'],
};
export function optionLabel(value: string, t: Translate): string { const labels = choiceNames[value]; return labels ? t(...labels) : value; }
const choices = (values: string[]) => values.map(value => [value, ...(choiceNames[value] || [value, value])] as [string, string, string]);
const qualityChoices: [string, string, string][] = [['best', '最高画质（自动）', 'Highest quality (automatic)'], ['worst', '最低画质（自动）', 'Lowest quality (automatic)'], ...choices(['8k', '4k', '2k', '1080p', '720p', '480p', '360p', '240p']), ['qcif', '144p', '144p']];
export function automaticQuality(options: DownloadOptions, quality: string): DownloadOptions {
  return { quality, contentMode: options.contentMode === 'audio' ? 'audio' : 'video', formatId: '', videoFormatId: '', audioFormatIds: [] };
}
function qualityValue(options: DownloadOptions): string {
  const manual = options.contentMode === 'manual' || options.formatId || options.videoFormatId || Array.isArray(options.audioFormatIds) && options.audioFormatIds.length > 0;
  return manual ? 'manual' : String(options.quality || 'best');
}
export function qualityLabel(options: DownloadOptions, t: Translate): string {
  const value = qualityValue(options);
  if (value === 'manual') return t('手动选格式', 'Choose format');
  const choice = qualityChoices.find(([id]) => id === value);
  return choice ? t(choice[1], choice[2]) : value;
}
export function QualitySelect({ options, change, t }: { options: DownloadOptions; change: (quality: string) => void; t: Translate }) {
  return <select value={qualityValue(options)} onChange={event => change(event.target.value)}><option value="manual" disabled>{t('手动选格式', 'Choose format')}</option>{qualityChoices.map(([id, zh, en]) => <option key={id} value={id}>{t(zh, en)}</option>)}</select>;
}
export const encoders = ['original', 'libx264', 'libx265', 'libvpx-vp9', 'libsvtav1', 'libaom-av1', 'librav1e', 'h264_nvenc', 'hevc_nvenc', 'av1_nvenc', 'h264_qsv', 'hevc_qsv', 'av1_qsv', 'h264_amf', 'hevc_amf', 'av1_amf'];
export const defaults: DownloadOptions = {
  cookieId: 'auto',
  contentMode: 'video', quality: 'best', audioFormat: 'best', audioTrack: 'default', container: 'auto', videoFps: 'best', sourceVideoCodec: 'auto', vrMode: 'any', compatibilityTarget: 'auto',
  collectionMode: 'select', collectionOrder: 'original', firstN: 0, playlistItems: '', dateAfter: '', dateBefore: '', durationMin: 0, durationMax: 0,
  liveFilter: 'videos', liveFromStart: false, waitForLive: false, waitMin: 30, waitMax: 120, sectionStart: '', sectionEnd: '', splitChapters: false,
  subtitleLanguage: 'none', subtitleFormat: 'srt', automaticSubtitles: true, skipUnavailableSubtitles: true, embedSubtitles: false, writeThumbnail: true, writeDescription: false, writeComments: false, writeInfoJson: false, embedMetadata: false, embedChapters: false, embedThumbnail: false,
  sponsorblockMode: 'off', sponsorblockCategories: ['sponsor'], rateLimit: '', encoder: 'original', organizeTaskFolder: true, filenameTemplate: '%(title)s.%(ext)s',
  convertCoverJpeg: true, coverJpegQuality: 90, prependCoverEnabled: false, prependCoverFrames: 3, fragmentConcurrent: 4, requestDelay: 0,
};
export function commandOptions(options: DownloadOptions): DownloadOptions {
  const { proxyConfigured: _proxyConfigured, ...value } = options;
  return { ...value, cookieId: 'auto', skipUnavailableSubtitles: value.skipUnavailableSubtitles ?? true, fragmentConcurrent: Math.min(8, Number(value.fragmentConcurrent ?? 4)) };
}
export const optionGroups: { key: string; title: [string, string]; fields: OptionField[] }[] = [
  { key: 'media', title: ['媒体格式与处理', 'Media format and processing'], fields: [
    { key: 'contentMode', zh: '内容类型', en: 'Content type', choices: [['video', '视频与音频', 'Video with audio'], ['audio', '仅音频', 'Audio only'], ['manual', '逐任务选择格式', 'Choose format per task']] },
    { key: 'quality', zh: '画质选择', en: 'Quality selection', choices: qualityChoices },
    { key: 'subtitleLanguage', zh: '字幕语言', en: 'Subtitle language', choices: [['none', '不下载字幕', 'No subtitles'], ...choices(['zh-Hans', 'zh-Hant', 'en', 'ja', 'ko', 'es', 'fr', 'de', 'ru', 'pt', 'ar', 'hi', 'id', 'vi', 'th', 'tr']), ['all', '全部语言', 'All languages']] },
    { key: 'automaticSubtitles', zh: '字幕来源', en: 'Subtitle source', type: 'boolean', choices: [['true', '人工优先，缺失用自动', 'Manual first, automatic fallback'], ['false', '仅人工字幕', 'Manual subtitles only']] },
    { key: 'skipUnavailableSubtitles', zh: '无字幕时继续下载', en: 'Continue when subtitles are unavailable', type: 'check' },
    { key: 'subtitleFormat', zh: '字幕文件格式', en: 'Subtitle file format', choices: choices(['srt', 'best', 'vtt', 'ass', 'lrc']) },
    { key: 'embedSubtitles', zh: '嵌入字幕', en: 'Embed subtitles', type: 'check' },
    { key: 'audioFormat', zh: '音频输出格式', en: 'Audio output format', choices: choices(['best', 'aac', 'alac', 'flac', 'm4a', 'mp3', 'opus', 'vorbis', 'wav']) },
    { key: 'audioTrack', zh: '音轨选择', en: 'Audio track selection', choices: [['default', '默认音轨', 'Default audio'], ['original', '原始音轨', 'Original audio'], ['all', '全部音轨', 'All tracks'], ...choices(['zh-Hans', 'zh-Hant', 'en', 'ja', 'ko', 'es', 'fr', 'de', 'ru', 'pt', 'ar', 'hi', 'id', 'vi', 'th', 'tr'])] },
    { key: 'container', zh: '视频容器', en: 'Video container', choices: [['auto', '自动 / 来源格式', 'Automatic / source format'], ...choices(['mp4', 'mkv'])] },
    { key: 'videoFps', zh: '帧率要求', en: 'Frame rate requirement', choices: choices(['best', '240', '120', '60', '50', '30', '25', '24']) },
    { key: 'sourceVideoCodec', zh: '来源编码偏好', en: 'Preferred source codec', choices: choices(['auto', 'h264', 'h265', 'av1', 'vp9']) },
    { key: 'vrMode', zh: 'VR 格式偏好', en: 'VR format preference', choices: [['any', '任意', 'Any'], ['none', '普通平面视频', 'Standard video'], ...choices(['2d360', '3d180', '3d360'])] },
    { key: 'compatibilityTarget', zh: '兼容设备', en: 'Device compatibility', choices: choices(['auto', 'windows', 'macos', 'linux', 'ios', 'android']) },
    { key: 'encoder', zh: '下载后视频编码器', en: 'Video encoder after download', choices: [['original', '保持原编码', 'Keep source codec'], ...choices(encoders.slice(1))], hint: ['硬件编码器需要兼容设备', 'Hardware encoders require compatible devices'] },
    { key: 'organizeTaskFolder', zh: '为每个任务创建文件夹', en: 'Create a folder for each task', type: 'check' },
    { key: 'filenameTemplate', zh: '文件名模板', en: 'Filename template', hint: ['yt-dlp 模板，例如 %(title)s.%(ext)s', 'yt-dlp template, e.g. %(title)s.%(ext)s'] },
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
  { key: 'extras', title: ['封面与元数据', 'Covers and metadata'], fields: [
    ...[['writeThumbnail', '保存来源封面', 'Save source cover'], ['writeDescription', '保存描述文件', 'Save description file'], ['writeComments', '保存评论', 'Save comments'], ['writeInfoJson', '保存媒体信息文件', 'Save media information file'], ['embedMetadata', '嵌入标题和描述', 'Embed title and description'], ['embedChapters', '嵌入章节', 'Embed chapters'], ['embedThumbnail', '嵌入封面', 'Embed cover'], ['convertCoverJpeg', '下载封面自动转为 JPEG', 'Convert downloaded covers to JPEG'], ['prependCoverEnabled', '在视频片头插入下载封面', 'Prepend the downloaded cover to video']].map(([key, zh, en]) => ({ key, zh, en, type: 'check' as const })),
    { key: 'coverJpegQuality', zh: 'JPEG 质量', en: 'JPEG quality', type: 'number', min: 50, max: 100 }, { key: 'prependCoverFrames', zh: '片头封面帧数', en: 'Cover intro frame count', type: 'number', min: 1, max: 300 },
  ] },
  { key: 'sponsor', title: ['SponsorBlock 与速度限制', 'SponsorBlock and speed limit'], fields: [
    { key: 'sponsorblockMode', zh: 'SponsorBlock 行为', en: 'SponsorBlock mode', choices: [['off', '关闭', 'Off'], ['mark', '标记片段', 'Mark segments'], ['remove', '移除片段', 'Remove segments']] },
    { key: 'rateLimit', zh: '下载速度上限', en: 'Download speed limit', hint: ['留空不限速；例如 5M、800K', 'Leave empty for no limit; e.g. 5M, 800K'] },
  ] },
];
export const OptionsForm = memo(function OptionsForm({ options, change, t, quick = false }: { options: DownloadOptions; change: (key: string, value: OptionValue) => void; t: Translate; quick?: boolean }) {
  const groups = optionGroups.map(group => ({ ...group, fields: group.fields.filter(field => !quick || ['contentMode', 'quality', 'audioFormat', 'container', 'encoder', 'collectionMode', 'subtitleLanguage', 'automaticSubtitles', 'skipUnavailableSubtitles', 'embedSubtitles', 'subtitleFormat', 'prependCoverEnabled'].includes(field.key)) })).filter(group => group.fields.length);
  return <>{groups.map(group => <details key={group.key} className="settings-fold panel" open={group.key === 'media'}><summary><strong>{t(...group.title)}</strong></summary><div className="form-grid">
    {group.fields.map(field => field.key === 'quality' ? <Field key={field.key} label={t(field.zh, field.en)}><QualitySelect options={options} change={quality => { for (const [key, value] of Object.entries(automaticQuality(options, quality))) change(key, value); }} t={t} /></Field> : <OptionControl key={field.key} field={field} value={field.key === 'subtitleLanguage' && Array.isArray(options.subtitleLanguages) && options.subtitleLanguages.length ? options.subtitleLanguages.join(',') : options[field.key] ?? defaults[field.key]} change={(key, value) => { if (key === 'subtitleLanguage') change('subtitleLanguages', []); change(key, value); }} t={t} />)}
  </div>{group.key === 'sponsor' && !quick && <div className="checkbox-grid">{['sponsor', 'intro', 'outro', 'selfpromo', 'interaction', 'preview', 'music_offtopic', 'filler'].map(category => <Check key={category} label={optionLabel(category, t)} checked={Array.isArray(options.sponsorblockCategories) && options.sponsorblockCategories.includes(category)} onChange={event => { const values = new Set(Array.isArray(options.sponsorblockCategories) ? options.sponsorblockCategories : []); event.target.checked ? values.add(category) : values.delete(category); change('sponsorblockCategories', [...values]); }} />)}</div>}</details>)}</>;
});
function OptionControl({ field, value, change, t }: { field: OptionField; value: OptionValue | undefined; change: (key: string, value: OptionValue) => void; t: Translate }) {
  const label = t(field.zh, field.en);
  if (field.type === 'check') return <Check label={label} checked={value === true} onChange={event => change(field.key, event.target.checked)} />;
  return <Field label={label} hint={field.hint && t(...field.hint)}>{field.choices ? <select value={String(value ?? '')} onChange={event => change(field.key, field.type === 'boolean' ? event.target.value === 'true' : event.target.value)}>{!field.choices.some(([id]) => id === String(value)) && value !== undefined && <option value={String(value)}>{t('旧设置', 'Saved value')}: {String(value)}</option>}{field.choices.map(([id, zh, en]) => <option key={id} value={id}>{t(zh, en)}</option>)}</select> : <input type={field.type || 'text'} value={String(value ?? '')} min={field.min} max={field.max} onChange={event => change(field.key, field.type === 'number' ? Number(event.target.value) : event.target.value)} />}</Field>;
}
