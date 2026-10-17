import { expect, it } from 'vitest';
import { bytes, dimensions, frameRate, mediaQuality } from './task';

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
});
