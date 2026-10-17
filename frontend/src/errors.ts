import type { Translate } from './i18n';

export function explainError(message: string, t: Translate): string {
  const missing = message.match(/^Subtitles unavailable for (.+)\. Video download will continue without these subtitles\.$/);
  if (missing) return t(`没有 ${missing[1]} 字幕，视频将继续下载。`, `Subtitles are unavailable for ${missing[1]}. The video will continue downloading.`);
  const hints: [RegExp, string, string][] = [
    [/liveFilter is set to videos/i, '当前方案仅下载非直播视频，这个来源正在直播或等待开播。请在“快速选项 → 直播筛选”选择“全部内容”，再重新添加链接。', 'This plan excludes live and upcoming streams. Under Quick options → Live stream filter, select All content and add the link again.'],
    [/liveFilter is set to live/i, '当前方案仅下载直播，这个来源是普通视频或已结束的直播回放。请将“直播筛选”改为“全部内容”，再重新添加链接。', 'This plan only includes live streams. The source is a regular video or finished recording. Select All content under Live stream filter and add the link again.'],
    [/Source duration is (?:shorter than durationMin|longer than durationMax)/i, '视频时长不符合当前方案的最小或最大时长限制。请在“快速选项 → 合集与筛选”调整时长后重新添加链接。', 'The video duration is outside this plan’s minimum or maximum. Adjust the duration under Quick options → Collections and filters and add the link again.'],
    [/Source upload date is (?:earlier than dateAfter|later than dateBefore)/i, '视频上传日期不在当前方案的日期范围内。请在“快速选项 → 合集与筛选”调整日期后重新添加链接。', 'The upload date is outside this plan’s date range. Adjust the dates under Quick options → Collections and filters and add the link again.'],
    [/^The login browser was closed, but the last Cookie sync failed:/i, '登录浏览器已关闭，但最后一次 Cookie 同步失败。此前已保存的 Cookie 会保留，请重新打开登录浏览器重试。', 'The login browser is closed, but its final cookie sync failed. Previously saved cookies are kept. Reopen the login browser to retry.'],
    [/^Could not refresh browser cookies:/i, '未能刷新浏览器 Cookie，将继续使用已保存且匹配网站的 Cookie；没有匹配记录时匿名访问。可重新打开登录浏览器重试同步。', 'Browser cookies could not be refreshed. Downloads continue with previously saved matching cookies, or anonymously when none are saved. Reopen the login browser to retry syncing.'],
    [/Session expired\. Sign in again\./i, '登录已过期，请重新登录。', 'Session expired. Sign in again.'],
    [/Service unavailable\. Check that the server is running and try again\./i, '服务暂不可用，请确认服务正在运行后重试。', 'Service unavailable. Check that the server is running and try again.'],
    [/Server returned an empty response\./i, '服务器返回了空响应，请重试。', 'Server returned an empty response. Please try again.'],
    [/Server returned an invalid response\./i, '服务器返回了无效响应，请重试。', 'Server returned an invalid response. Please try again.'],
    [/Request failed \(HTTP \d+\)\. Please try again\./i, '请求失败，请稍后重试。', 'Request failed. Please try again.'],
    [/Local file paths are available in the desktop app only\./i, '仅桌面端可以复制本地文件路径。', 'Local file paths are available in the desktop app only.'],
    [/Clipboard is unavailable\. Allow clipboard access and try again\./i, '剪贴板不可用，请允许剪贴板访问后重试。', 'Clipboard is unavailable. Allow clipboard access and try again.'],
    [/Unsupported release URL\./i, '不支持此发行版地址，请使用本项目的 GitHub 发行版页面。', 'Unsupported release URL.'],
    [/Model list authentication failed|Invalid AI API key/i, '模型列表鉴权失败，请检查 API 密钥及其权限。', 'Model list authentication failed. Check the API key and its permissions.'],
    [/does not support listing models/i, '此服务不支持获取模型列表，请手动填写模型 ID。', 'This service does not support listing models. Enter the model ID manually.'],
    [/Cannot fetch AI models|AI model service returned HTTP/i, '获取模型失败，请检查图像 API 地址和服务连接后重试。', 'Model discovery failed. Check the image API endpoint and service connection, then retry.'],
    [/No cookies to save|Open a login browser before saving/i, '请先打开登录浏览器，完成登录后 Cookie 会自动同步。', 'Open the login browser and complete sign-in. Cookies will sync automatically.'],
    [/Login window .*closed|Browser login session ended/i, '请重新打开登录浏览器以同步 Cookie；原登录状态仍按网站有效期保留。', 'Reopen the login browser to sync cookies; sign-in persists according to the site’s expiry settings.'],
    [/Install Edge, Chrome|Cannot start compatible sign-in|compatible sign-in browser startup timed out/i, '兼容登录浏览器无法启动。请确认已安装 Edge、Chrome 或 Chromium，并关闭占用应用登录配置的窗口。', 'Compatible sign-in could not start. Check that Edge, Chrome or Chromium is installed and close any window using the app’s login profile.'],
    [/runtime release not found/i, '所选通道没有此版本。请核对完整的官方 Release 标签，或留空安装最新版本。', 'This release is not available in the selected channel. Check the exact official release tag, or leave it blank for the latest release.'],
    [/runtime release.*(?:HTTP (?:403|429))|(?:runtime|yt-dlp release).*github.*HTTP (?:403|429)/i, 'GitHub 限制了组件更新请求。请稍后重试；下载网站的 Cookie 不能解决此问题。', 'GitHub has limited runtime update requests. Retry later; source-site cookies do not resolve this issue.'],
    [/Cannot check latest yt-dlp release|GitHub did not resolve the latest runtime release/i, '未能确认 GitHub 最新版本。请检查网络后重试；检查失败不代表已是最新。', 'The latest GitHub version could not be confirmed. Check connectivity and retry; a failed check does not mean the installed version is current.'],
    [/runtime release lacks a trusted checksum/i, '此版本缺少可信的官方校验值，已停止安装。请选择其他官方版本。', 'Installation stopped because this release has no trusted official checksum. Choose another official release.'],
    [/incompatible with required application options/i, '此 yt-dlp 版本不支持应用所需参数，已保留原组件。请选择更新的官方版本。', 'This yt-dlp release does not support the required options. The installed component was preserved; choose a newer official release.'],
    [/checksum mismatch/i, '工具或下载包完整性校验失败，已停止执行。请重新下载组件；旧任务需要恢复其原工具版本，或重新添加链接创建新任务。', 'Integrity verification failed and execution stopped. Download the component again; existing tasks need their original tools restored, or add the link as a new task.'],
    [/Runtime download failed/i, '组件下载失败。请检查所选 GitHub / CDN 来源的网络连接，或切换下载来源后重试。', 'The component download failed. Check connectivity to the selected GitHub / CDN source, or switch download sources and retry.'],
    [/runtime.*(?:github|download failed)|connect to github/i, '组件更新需要连接 GitHub，不读取“下载代理”设置。请检查 GitHub 网络连接后重试。', 'Runtime updates require access to GitHub and do not use the download proxy setting. Check GitHub connectivity and retry.'],
    [/HTTP(?: Error)? 429|too many requests/i, '来源网站限制了当前出口的请求。请稍后重试；可在网络设置中增大请求间隔后重新添加链接。', 'The source site has limited requests from this connection. Retry later, or increase the request delay in network settings and add the link again.'],
    [/Fresh cookies(?: \(not necessarily logged in\))? are needed/i, '来源网站需要较新的 Cookie，不一定需要登录。请在“网络与 Cookie”的登录浏览器中重新打开目标网页，等待 Cookie 自动同步后重新添加链接；若仍报错，可能是提取器或网站限制。', 'The source site needs fresh cookies, which do not necessarily require sign-in. Under Network, reopen the target webpage in the login browser, let cookies sync automatically, then add the link again. If it still fails, this may be an extractor or site limitation.'],
    [/confirm.*bot|sign[ -]?in|logged[ -]in|login required|authentication required/i, '来源网站要求登录或验证。请在“网络与 Cookie”打开浏览器登录，Cookie 会自动同步，再重新添加链接；已有任务保留原身份。', 'The source site requires sign-in or verification. Open browser sign-in under Network; cookies sync automatically. Add the link again; existing tasks keep their original credentials.'],
    [/HTTP(?: Error)? 403/i, '来源网站拒绝访问。请检查访问权限、Cookie 和下载代理；修改身份设置后需重新添加链接。', 'The source site refused access. Check permissions, cookies, and the download proxy; add the link again after changing credentials.'],
    [/Selected format .*no longer available|No (?:downloadable|video|audio).*format|No audio stream|Requested subtitles.*unavailable/i, '当前视频没有符合方案的格式或字幕。请重新解析，并调整画质、格式或字幕选择。', 'The video has no matching format or subtitles. Read its formats again and adjust quality, format, or subtitle choices.'],
    [/no such option|unrecognized (?:option|argument)/i, '当前工具不支持应用所需参数。请更新运行时组件后重新添加链接；Docker 需更新镜像。', 'The tool does not support the required options. Update runtime components and add the link again; Docker installations require an updated image.'],
    [/is not installed|Cannot start (?:yt-dlp|ffmpeg|ffprobe|deno|qjs)/i, '运行时组件缺失或无法启动。请在“工具与更新”检查或重新安装组件；Docker 需检查镜像内组件。', 'A runtime component is missing or cannot start. Check or reinstall it under Tools & updates; Docker installations must check the image components.'],
    [/timed? ?out|timeout|connection refused|unable to download webpage/i, '连接来源网站失败。请检查网络和下载代理后重试。', 'Connection to the source site failed. Check the network and download proxy, then retry.'],
  ];
  const hint = hints.find(([pattern]) => pattern.test(message));
  return hint ? `${t(hint[1], hint[2])} ${t('详细错误：', 'Details: ')}${message}` : message;
}
