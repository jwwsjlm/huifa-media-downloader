import { expect, it } from 'vitest';
import { bytes, completionTime, dimensions, frameRate, mediaQuality, mediaQualityWithFps, taskSize, taskActions } from './task';
import { makeTask } from './test-fixtures';

it('formats real completion times locally to minutes using case-sensitive tokens', () => {
  const value = new Date(2026, 9, 18, 23, 7, 59).toISOString();
  expect(completionTime(value)).toBe('2026-10-18 23:07');
  expect(completionTime(value, 'YYMMDD HH:mm')).toBe('261018 23:07');
  expect(completionTime(value, 'YYYY年MM月DD日 HH:mm')).toBe('2026年10月18日 23:07');
  expect(completionTime(value, '')).toBe('2026-10-18 23:07');
  for (const missing of [undefined, null, '', 'invalid']) expect(completionTime(missing)).toBe('');
});

it.each([
  [3840, 2160, '4K'], [2160, 3840, '4K'], [3840, 1600, '4K'],
  [1920, 1080, '1080p'], [1080, 1920, '1080p'], [720, 1280, '720p'], [2560, 1440, '1440p'],
])('labels actual %s × %s resolution as %s in either orientation', (width, height, quality) => {
  expect(mediaQuality({ width, height })).toBe(quality);
  expect(dimensions({ width, height })).toBe(`${width} × ${height}`);
});
it('keeps absent actual metadata unknown and formats file bytes and fractional FPS', () => {
  expect(mediaQuality()).toBe('—'); expect(mediaQuality({ height: 2160 })).toBe('—');
  expect(dimensions(null)).toBe('—'); expect(frameRate(29.970029)).toBe('29.97 fps');
  expect(frameRate(null)).toBe('—'); expect(bytes(10485760)).toBe('10.0 MiB');
  expect(bytes(0.25)).toBe('0 B');
});
it.each([[59.94, '1080p60'], [60, '1080p60'], [50, '1080p50'], [29.97, '1080p'], [25, '1080p'], [null, '1080p'], [Infinity, '1080p']])('shows actual high frame rate %s beside quality as %s', (fps, label) => {
  expect(mediaQualityWithFps({ width: 1920, height: 1080, fps })).toBe(label);
});
it('does not infer a resolution from FPS alone or change the detailed resolution label', () => {
  expect(mediaQualityWithFps({ fps: 60 })).toBe('—');
  expect(mediaQualityWithFps({ width: 3840, height: 2160, fps: 59.94 })).toBe('4K60');
  expect(mediaQuality({ width: 3840, height: 2160, fps: 59.94 })).toBe('4K');
});
it('shows actual downloaded bytes with known, estimated and unknown totals, preserving final output sizes', () => {
  const en = (_zh: string, english: string) => english;
  const task = { ...makeTask('a'), downloadedBytes: 1048576, size: 999999999 };
  expect(taskSize(task, en)).toEqual({ value: '1.0 MiB', detail: 'Downloaded', title: 'Total size unknown; showing downloaded bytes' });
  expect(taskSize({ ...task, totalBytes: 2097152 }, en)).toEqual({ value: '1.0 MiB', detail: '/ 2.0 MiB', title: 'Downloaded / total: 1.0 MiB / 2.0 MiB' });
  expect(taskSize({ ...task, totalBytes: 2097152, totalBytesEstimated: true }, en).detail).toBe('/ ≈ 2.0 MiB');
  expect(taskSize({ ...task, downloadedBytes: 0 }, en).value).toBe('0 B');
  for (const downloadedBytes of [undefined, null, -1, Infinity]) expect(taskSize({ ...task, downloadedBytes }, en)).toEqual({ value: '—' });
  expect(taskSize({ ...task, status: 'paused' }, en).value).toBe('1.0 MiB');
  expect(taskSize({ ...task, status: 'completed', size: 3145728 }, en)).toEqual({ value: '3.0 MiB' });
  expect(taskSize({ ...task, status: 'retained', size: undefined, mediaInfo: { size: 4194304 } }, en)).toEqual({ value: '4.0 MiB' });
});
it('offers live save separately from cancel, current settings retry and real queue priority only where applicable', () => {
  const actions = (patch: Partial<ReturnType<typeof makeTask>>) => taskActions({ ...makeTask('a'), ...patch }, (_zh, en) => en).map(item => item.action);
  expect(actions({ status: 'downloading', live: true })).toContain('finishLive');
  expect(actions({ status: 'downloading', live: false })).not.toContain('finishLive');
  expect(actions({ status: 'queued' })).toContain('prioritize');
  expect(actions({ status: 'downloading' })).not.toContain('prioritize');
  expect(actions({ status: 'failed' })).toContain('retryCurrent');
  expect(actions({ status: 'failed', conversion: true })).not.toContain('retryCurrent');
  expect(actions({ status: 'failed', kind: 'assets' })).not.toContain('retryCurrent');
  expect(actions({ status: 'failed', error: 'Requirements unmet: missing subtitles' })).toContain('configure');
});
