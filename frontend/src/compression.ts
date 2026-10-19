import type { Translate } from './i18n';

export type CompressionOptions = {
  targetBytes?: number;
  videoBitrateBps?: number;
  maxWidth?: number;
  maxHeight?: number;
  fps?: string;
  encoder?: string;
};
export interface CompressionPreview {
  source: {
    sizeBytes: number; duration: number; width: number; height: number; displayWidth: number; displayHeight: number;
    fps: string | null; variableFrameRate?: boolean; videoBitrateBps: number | null; videoBitrateEstimated: boolean;
    hdr: 'sdr' | 'hdr10' | 'hlg' | 'unsupported'; bitDepth: number; audioTracks: number; subtitleTracks: number; attachmentTracks: number;
  };
  plan: { encoder: string; videoBitrateBps: number; estimatedSizeBytes: number; width: number; height: number; fps: string | null; targetBytes?: number } | null;
  capabilities: { encoder: string; label: string; available: boolean; reason: string | null }[];
  blockingReasons: string[];
  warnings: string[];
}

export function compressionReason(reason: string, t: Translate): string {
  const messages: [string, string, string][] = [
    ['Dolby Vision or dynamic HDR', '暂不支持完整保留杜比视界或动态 HDR，无法压缩此视频。', 'Dolby Vision or dynamic HDR cannot be fully preserved, so this video cannot be compressed.'],
    ['contains a data stream or attached picture', '此视频含有无法完整保留的附加数据或内嵌封面，无法压缩。', 'Additional data or an embedded picture cannot be preserved, so this video cannot be compressed.'],
    ['subtitle codec cannot be copied into MKV', '此视频的字幕格式无法原样保留到 MKV，暂时无法压缩。', 'The subtitle format cannot be copied into MKV unchanged, so compression is unavailable.'],
    ['pixel format, bit depth or alpha channel', 'HEVC 无法完整保留此视频的像素格式、位深或透明通道。', 'HEVC cannot preserve this video’s pixel format, bit depth or alpha channel.'],
    ['Interlaced video or changing frame properties', '暂不支持隔行视频或处理期间画面参数变化的视频。', 'Interlaced video or changing frame properties are currently unsupported.'],
    ['selected encoder cannot preserve', '所选编码器无法保留此视频的格式和特性，请选择可用编码器。', 'The selected encoder cannot preserve this video’s format and features. Choose an available encoder.'],
    ['not available on this platform', '此编码器不支持当前平台。', 'This encoder is unavailable on this platform.'],
    ['GPU path cannot preserve', 'GPU 无法保留此视频的像素格式或 HDR 元数据，请使用 CPU。', 'GPU encoding cannot preserve this video’s pixel format or HDR metadata. Use CPU.'],
    ['installed NVIDIA driver does not support', 'NVIDIA 驱动与当前 FFmpeg 不兼容。请在“设置→工具与更新”应用兼容版 FFmpeg，也可升级驱动或使用 CPU。', 'The NVIDIA driver is incompatible with this FFmpeg build. Apply the Compatibility FFmpeg build under Settings → Tools & updates, or update the driver or use CPU.'],
    ['Encoder capability probe timed out', '编码器检测超时，请稍后重试或使用 CPU。', 'Encoder detection timed out. Try again or use CPU.'],
    ['encoder cannot preserve the source pixel format', '此编码器无法保留源视频的像素格式。', 'This encoder cannot preserve the source pixel format.'],
    ['encoder could not initialize', '当前设备无法对此视频启用该编码器，请检查驱动或使用 CPU。', 'This encoder could not initialize for the video. Check the driver or use CPU.'],
    ['Video compression is lossy.', '视频压缩会损失画质，音轨、字幕、附件和章节将复制保留到 MKV。', 'Video compression is lossy. Audio, subtitles, attachments and chapters are copied into MKV.'],
  ];
  const message = messages.find(([prefix]) => reason.toLowerCase().includes(prefix.toLowerCase()));
  return message ? t(message[1], message[2]) : reason;
}

function decimalValue(number: string, multiplier: number): number | null {
  const [whole, fraction = ''] = number.split('.');
  const value = BigInt((whole || '0') + fraction) * BigInt(multiplier) / 10n ** BigInt(fraction.length);
  return value > 0n && value <= BigInt(Number.MAX_SAFE_INTEGER) ? Number(value) : null;
}
export function parseSize(text: string): number | null {
  if (text.length > 48) return null;
  const match = text.trim().match(/^(\d+(?:\.\d+)?|\.\d+)\s*(b|kb|kib|k|mb|mib|m|gb|gib|g)?$/i);
  if (!match) return null;
  const unit = (match[2] || 'mb').toLowerCase();
  const base = unit.includes('i') ? 1024 : 1000;
  return decimalValue(match[1], unit === 'b' ? 1 : base ** (unit.startsWith('k') ? 1 : unit.startsWith('g') ? 3 : 2));
}
export function parseVideoBitrate(text: string): number | null {
  if (text.length > 48) return null;
  const match = text.trim().match(/^(\d+(?:\.\d+)?|\.\d+)\s*(bps|kbps|k|mbps|m)?$/i);
  if (!match) return null;
  const unit = (match[2] || 'mbps').toLowerCase();
  return decimalValue(match[1], unit === 'bps' ? 1 : unit.startsWith('k') ? 1000 : 1000000);
}
export function formatSize(value: number): string {
  const divisor = value >= 1000000000 ? 1000000000 : value >= 1000000 ? 1000000 : value >= 1000 ? 1000 : 1;
  return `${Number((value / divisor).toFixed(2))} ${divisor === 1000000000 ? 'GB' : divisor === 1000000 ? 'MB' : divisor === 1000 ? 'KB' : 'B'}`;
}
export function sizeInput(value: number): string { return `${Number((value / 1000000).toFixed(6))} MB`; }
export function formatBitrate(value: number): string { return `${Number((value / 1000000).toFixed(3))} Mbps`; }
export function resolutionChoices(width: number, height: number) {
  const choices = [{ value: 'original', label: `${width} × ${height}`, width, height }];
  for (const [value, long, short] of [['2160', 3840, 2160], ['1440', 2560, 1440], ['1080', 1920, 1080], ['720', 1280, 720], ['480', 854, 480]] as const) {
    const scale = Math.min((width >= height ? long : short) / width, (width >= height ? short : long) / height);
    if (scale >= 1) continue;
    const targetWidth = Math.max(2, Math.floor(width * scale / 2) * 2), targetHeight = Math.max(2, Math.floor(height * scale / 2) * 2);
    if (choices.some(choice => choice.width === targetWidth && choice.height === targetHeight)) continue;
    choices.push({ value, label: `${value}p · ${targetWidth} × ${targetHeight}`, width: targetWidth, height: targetHeight });
  }
  return choices;
}
export function frameRateChoices(source: string | null) {
  const choices = [{ value: 'original', label: source ? `${Number(rationalRate(source).toFixed(3))} fps` : '' }];
  if (!source || !Number.isFinite(rationalRate(source))) return choices;
  for (const value of ['120', '60', '60000/1001', '50', '30', '30000/1001', '25', '24', '24000/1001']) {
    if (rationalRate(value) < rationalRate(source) - 0.001) choices.push({ value, label: `${Number(rationalRate(value).toFixed(3))} fps` });
  }
  return choices;
}
function rationalRate(value: string): number { const [numerator, denominator = '1'] = value.split('/'); return Number(numerator) / Number(denominator); }
export function estimateSize(videoBitrateBps: number, duration: number, reference: { videoBitrateBps: number; estimatedSizeBytes: number }): number {
  return Math.max(1, Math.ceil(reference.estimatedSizeBytes + (videoBitrateBps - reference.videoBitrateBps) * duration / 8));
}
