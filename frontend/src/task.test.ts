import { expect, it } from 'vitest';
import { bytes, dimensions, frameRate, mediaQuality, mediaQualityWithFps } from './task';

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
