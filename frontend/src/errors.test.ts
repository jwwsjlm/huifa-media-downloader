import { expect, it } from 'vitest';
import { explainError } from './errors';

const en = (_zh: string, english: string) => english;
it.each([
  ['yt-dlp.exe exited with code 1: Sign in to confirm you are not a bot', 'existing tasks keep their original credentials'],
  ['The web client only works when logged-in', 'Open browser sign-in'],
  ['HTTP Error 429: Too Many Requests', 'request delay'],
  ['Cannot query yt-dlp runtime release from GitHub: HTTP 403', 'GitHub has limited'],
  ['Cannot check latest yt-dlp release from GitHub: HTTP 429', 'GitHub has limited'],
  ['Cannot check latest yt-dlp release from GitHub: timeout', 'failed check does not mean'],
  ['Runtime download failed: ghfast.top: HTTP 502', 'switch download sources'],
  ['Runtime release not found for yt-dlp', 'exact official release tag'],
  ['Runtime release lacks a trusted checksum; installation refused', 'no trusted official checksum'],
  ['This yt-dlp version is incompatible with required application options; no such option: --no-js-runtimes', 'installed component was preserved'],
  ['Frozen yt-dlp runtime checksum mismatch', 'original tools restored'],
  ['Cannot download yt-dlp runtime from GitHub: connection timed out', 'do not use the download proxy'],
  ['Selected format 137 is no longer available', 'adjust quality'],
  ['No video format satisfies the selected resolution', 'adjust quality'],
  ['Requested subtitles zh are unavailable', 'subtitle choices'],
  ['no such option: --no-js-runtimes', 'add the link again'],
])('explains %s while retaining diagnostic details', (raw, hint) => {
  const message = explainError(raw, en);
  expect(message).toContain(hint);
  expect(message.endsWith(raw)).toBe(true);
});
it('leaves unknown errors intact and supports Chinese guidance', () => {
  expect(explainError('disk fixture failure', en)).toBe('disk fixture failure');
  expect(explainError('HTTP Error 429', zh => zh)).toContain('来源网站限制');
});
it.each([
  ['Session expired. Sign in again.', '登录已过期'],
  ['Service unavailable. Check that the server is running and try again.', '服务暂不可用'],
  ['Server returned an empty response. Please try again.', '空响应'],
  ['Server returned an invalid response. Please try again.', '无效响应'],
  ['Request failed (HTTP 502). Please try again.', '请求失败'],
  ['Local file paths are available in the desktop app only.', '仅桌面端'],
  ['Clipboard is unavailable. Allow clipboard access and try again.', '剪贴板不可用'],
  ['Unsupported release URL.', '不支持此发行版地址'],
])('explains transport error %s in Chinese before source-site rules', (raw, hint) => {
  const message = explainError(raw, chinese => chinese);
  expect(message).toContain(hint);
  expect(message.endsWith(raw)).toBe(true);
  expect(message).not.toContain('来源网站');
});
