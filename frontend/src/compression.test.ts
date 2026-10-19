import { describe, expect, it } from 'vitest';
import { compressionReason, frameRateChoices, estimateSize, formatBitrate, formatSize, parseSize, parseVideoBitrate, resolutionChoices } from './compression';

describe('compression inputs', () => {
  it.each([['4MB', 4000000], ['4GB', 4000000000], [' 4g ', 4000000000], ['1.25 GB', 1250000000], ['.5m', 500000], ['4', 4000000], ['0.000001 MB', 1]])('parses decimal size %s exactly', (text, bytes) => {
    expect(parseSize(text)).toBe(bytes);
  });
  it.each([['4MiB', 4194304], ['4GiB', 4294967296], ['1.5 GiB', 1610612736]])('accepts explicit binary size %s without changing decimal MB semantics', (text, bytes) => {
    expect(parseSize(text)).toBe(bytes);
  });
  it.each(['', '0', '-4MB', 'Infinity', 'NaN', '4XB', '1e3MB', '4,5GB', '9007199254740992B', '0.0000001MB'])('rejects invalid or unsafe size %s', text => {
    expect(parseSize(text)).toBeNull();
  });
  it('keeps file bytes and video bits per second in separate decimal units', () => {
    expect(parseVideoBitrate('4 Mbps')).toBe(4000000);
    expect(parseVideoBitrate('800 Kbps')).toBe(800000);
    expect(parseVideoBitrate('2.5')).toBe(2500000);
    expect(parseVideoBitrate('4MB')).toBeNull();
    expect(formatBitrate(2500000)).toBe('2.5 Mbps');
    expect(formatSize(4000000000)).toBe('4 GB');
  });
  it('offers only smaller sizes and keeps portrait and cinematic aspect ratios', () => {
    expect(resolutionChoices(2160, 3840)).toEqual([
      { value: 'original', label: '2160 × 3840', width: 2160, height: 3840 },
      { value: '1440', label: '1440p · 1440 × 2560', width: 1440, height: 2560 },
      { value: '1080', label: '1080p · 1080 × 1920', width: 1080, height: 1920 },
      { value: '720', label: '720p · 720 × 1280', width: 720, height: 1280 },
      { value: '480', label: '480p · 480 × 852', width: 480, height: 852 },
    ]);
    const wide = resolutionChoices(3840, 1600);
    expect(wide.find(choice => choice.value === '1440')).toMatchObject({ width: 2560, height: 1066 });
    expect(resolutionChoices(640, 360)).toHaveLength(1);
    expect(resolutionChoices(1280, 720).every(choice => choice.width <= 1280 && choice.height <= 720)).toBe(true);
  });
  it('keeps the source rational rate and never offers a higher rounded frame rate', () => {
    expect(frameRateChoices('30000/1001').map(choice => choice.value)).toEqual(['original', '25', '24', '24000/1001']);
    expect(frameRateChoices('60000/1001').map(choice => choice.value)).toContain('30000/1001');
    expect(frameRateChoices('60000/1001').map(choice => choice.value)).not.toContain('60');
  });
  it('estimates bitrate output using the measured source plan without claiming a hard limit', () => {
    expect(estimateSize(2000000, 60, { videoBitrateBps: 1000000, estimatedSizeBytes: 9000000 })).toBe(16500000);
  });
  it('explains unsupported drivers and preservation limits in the current language', () => {
    const driverReason = 'The installed NVIDIA driver does not support this FFmpeg NVENC API; update the driver or use CPU';
    expect(compressionReason(driverReason, zh => zh)).toContain('设置→工具与更新”应用兼容版 FFmpeg');
    expect(compressionReason(driverReason, (_zh, en) => en)).toContain('Apply the Compatibility FFmpeg build under Settings → Tools & updates');
    expect(compressionReason('A subtitle codec cannot be copied into MKV without changing its format', zh => zh)).toContain('字幕格式无法原样保留');
    expect(compressionReason('Encoder capability probe timed out; try again or use CPU', zh => zh)).toContain('检测超时');
    expect(compressionReason('Custom safe reason', (_zh, en) => en)).toBe('Custom safe reason');
  });
});
