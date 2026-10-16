import { describe, expect, it } from 'vitest';
import { downloadLinks, singleVideoLink } from './links';
describe('download links', () => {
  it('extracts and deduplicates clipboard URLs while rejecting credentials and empty input', () => {
    expect(downloadLinks('视频 https://youtu.be/abcdefghijk，\nhttps://youtu.be/abcdefghijk\nhttps://www.bilibili.com/video/BV1234567890/')).toEqual(['https://youtu.be/abcdefghijk', 'https://www.bilibili.com/video/BV1234567890/']);
    expect(() => downloadLinks('not a URL')).toThrow('No HTTP');
    expect(() => downloadLinks('https://name:password@example.com/a')).toThrow('without login credentials');
    expect(() => downloadLinks(Array.from({ length: 1001 }, (_, i) => `https://example.com/${i}`).join('\n'))).toThrow('1000');
  });
  it('distinguishes explicit videos, mixed video/playlist links, collections and redirects', () => {
    for (const url of ['https://www.youtube.com/watch?v=abcdefghijk&list=PL123', 'https://youtu.be/abcdefghijk', 'https://www.youtube.com/shorts/abcdefghijk', 'https://www.bilibili.com/video/BV1234567890/?p=2', 'https://example.com/video.mp4?token=fixture']) expect(singleVideoLink(url)).toBe(true);
    for (const url of ['https://www.youtube.com/playlist?list=PL123', 'https://www.youtube.com/@example/videos', 'https://b23.tv/example', 'https://www.youtube.com.evil.test/watch?v=abcdefghijk']) expect(singleVideoLink(url)).toBe(false);
  });
});
