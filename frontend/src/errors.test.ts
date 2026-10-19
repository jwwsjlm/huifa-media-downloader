import { expect, it } from 'vitest';
import { explainError } from './errors';

const en = (_zh: string, english: string) => english;
it.each([
  ['Source is live or upcoming, but liveFilter is set to videos. Choose All content to download live streams.', 'Quick options → Live stream filter'],
  ['Source is not live or upcoming, but liveFilter is set to live. Choose All content to download recordings.', 'regular video or finished recording'],
  ['Source duration is longer than durationMax.', 'minimum or maximum'],
  ['Source upload date is earlier than dateAfter.', 'date range'],
  ['yt-dlp.exe exited with code 1: Sign in to confirm you are not a bot', 'existing tasks keep their original credentials'],
  ['The web client only works when logged-in', 'Open browser sign-in'],
  ['HTTP Error 429: Too Many Requests', 'request delay'],
  ['Cannot query yt-dlp runtime release from GitHub: HTTP 403', 'GitHub has limited'],
  ['Cannot check latest yt-dlp release from GitHub: HTTP 429', 'GitHub has limited'],
  ['Cannot query official FFmpeg release: HTTP 403', 'GitHub has limited'],
  ['Cannot download official FFmpeg checksums: HTTP 429', 'GitHub has limited'],
  ['Cannot query official FFmpeg release: HTTP 404', 'recommended release'],
  ['Cannot query official FFmpeg release: timeout', 'official FFmpeg release or checksums'],
  ['This release has no matching FFmpeg build for the selected mode and platform', 'current system and policy'],
  ['Cannot check latest yt-dlp release from GitHub: timeout', 'failed check does not mean'],
  ['Runtime download failed: ghfast.top: HTTP 502', 'switch download sources'],
  ['Runtime release not found for yt-dlp', 'exact official release tag'],
  ['Runtime release lacks a trusted checksum; installation refused', 'no trusted official checksum'],
  ['This yt-dlp version is incompatible with required application options; no such option: --no-js-runtimes', 'installed component was preserved'],
  ['Frozen yt-dlp runtime checksum mismatch', 'original tools restored'],
  ['Cannot download yt-dlp runtime from GitHub: connection timed out', 'saved download proxy'],
  ['Selected format 137 is no longer available', 'adjust quality'],
  ['No video format satisfies the selected resolution', 'adjust quality'],
  ['Requested subtitles zh are unavailable', 'requested subtitles are unavailable'],
  ['no such option: --no-js-runtimes', 'add the link again'],
  ['GPU compression failed; retry this task on CPU: driver timeout at C:/private/video.mkv', 'Choose Retry with CPU'],
  ['Compressed output still exceeds the target size; choose a larger target', 'Adjust it and retry'],
  ['Source changed during compression; the original file was preserved', 'Create a new compression task'],
  ['Compression lost a subtitle stream', 'It was not published'],
])('explains %s without appending diagnostic details', (raw, hint) => {
  const message = explainError(raw, en);
  expect(message).toContain(hint);
  expect(message).not.toContain(raw);
  expect(message).not.toContain('Details:');
});
it('summarizes unknown errors and supports Chinese guidance', () => {
  expect(explainError('disk fixture failure', en)).toBe('The operation could not be completed. View the logs for details.');
  expect(explainError('disk fixture failure', zh => zh)).toBe('操作未完成，请查看日志了解详情。');
  expect(explainError('HTTP Error 429', zh => zh)).toContain('来源网站限制');
});
it('explains fresh-cookie requirements without treating not-logged-in as mandatory sign-in', () => {
  const raw = 'ERROR: [Douyin] 123: Fresh cookies (not necessarily logged in) are needed';
  const message = explainError(raw, en);
  expect(message).toContain('do not necessarily require sign-in');
  expect(message).toContain('reopen the target webpage');
  expect(message).toContain('extractor or site limitation');
  expect(message).not.toContain('source site requires sign-in');
  expect(message).not.toContain(raw);
  const chinese = explainError(raw, zh => zh);
  expect(chinese).toContain('不一定需要登录');
  expect(chinese).not.toContain(raw);
});
it.each([
  ['The login browser was closed, but the last Cookie sync failed: native Cookie read timeout. Previously saved cookies were kept. Reopen the login browser to retry.', '登录浏览器已关闭，但最后一次 Cookie 同步失败。此前已保存的 Cookie 会保留，请重新打开登录浏览器重试。', 'The login browser is closed, but its final cookie sync failed. Previously saved cookies are kept. Reopen the login browser to retry.'],
  ['Could not refresh browser cookies: Sign in again. Continuing with previously saved matching cookies, or anonymously if none are saved.', '未能刷新浏览器 Cookie，将继续使用已保存且匹配网站的 Cookie；没有匹配记录时匿名访问。可重新打开登录浏览器重试同步。', 'Browser cookies could not be refreshed. Downloads continue with previously saved matching cookies, or anonymously when none are saved. Reopen the login browser to retry syncing.'],
])('explains cookie sync recovery before nested source-site errors: %s', (raw, chinese, english) => {
  const translated = explainError(raw, en);
  expect(translated).toBe(english);
  expect(explainError(raw, zh => zh)).toBe(chinese);
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
  expect(message).not.toContain(raw);
  expect(message).not.toContain('来源网站');
});

it('shows a short network hint for SSL EOF instead of tool commands and local paths', () => {
  const raw = 'Failed to parse video with yt-dlp: yt-dlp.exe exited with code 1: [debug] Command-line config: [--cookies, C:\\Users\\fixture\\cookies.txt]\nERROR: [youtube] fixture: Unable to download webpage: [SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol';
  expect(explainError(raw, chinese => chinese)).toBe('连接来源网站失败。请检查网络和下载代理后重试。');
  expect(explainError(raw, en)).toBe('Connection to the source site failed. Check the network and download proxy, then retry.');
});

it('does not mistake debug command flags for the failure reason', () => {
  const raw = 'yt-dlp.exe exited with code 1: [debug] Command-line config: [--socket-timeout, 30, --cookies, C:\\fixture\\sign-in.txt]\nERROR: An unrecognized extraction failure';
  expect(explainError(raw, chinese => chinese)).toBe('操作未完成，请查看日志了解详情。');
});

it('explains missing subtitles without showing the raw requirement failure', () => {
  const raw = 'Requirements unmet: subtitles=zh-Hans. Adjust the subtitle languages or disable subtitles before retrying';
  const message = explainError(raw, chinese => chinese);
  expect(message).toContain('来源没有所选字幕');
  expect(message).toContain('无字幕时继续下载');
  expect(message).not.toContain('Requirements unmet');
});

it('keeps concise Chinese validation messages and removes appended raw details', () => {
  expect(explainError('一次最多添加 1000 个链接', chinese => chinese)).toBe('一次最多添加 1000 个链接');
  expect(explainError('请输入有效文件名。详细错误：Invalid filename: C:\\Users\\fixture', chinese => chinese)).toBe('请输入有效文件名。');
  expect(explainError('Section end must follow start', chinese => chinese)).toBe('片段结束时间必须晚于开始时间。');
  expect(explainError('Invalid subtitleLanguage: invalid', chinese => chinese)).toContain('下载设置无效');
});

it('does not expose raw unknown payloads in a Chinese error prefix', () => {
  const raw = '操作失败：yt-dlp.exe exited with code 1: unknown failure at C:\\Users\\fixture';
  expect(explainError(raw, chinese => chinese)).toBe('操作未完成，请查看日志了解详情。');
});

it('explains the completion time format byte limit without raw validation output', () => {
  const raw = 'Completion date format must be 1–80 bytes without control characters';
  expect(explainError(raw, chinese => chinese)).toBe('完成时间格式须为 1–80 个字节的文本，且不能包含换行等控制字符。');
  expect(explainError(raw, en)).toBe('The completion time format must be 1–80 bytes without control characters.');
});
