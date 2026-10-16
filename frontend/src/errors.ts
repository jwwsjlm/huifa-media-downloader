import type { Translate } from './i18n';

export function explainError(message: string, t: Translate): string {
  const hints: [RegExp, string, string][] = [
    [/runtime release not found/i, '所选通道没有此版本。请核对完整的官方 Release 标签，或留空安装最新版本。', 'This release is not available in the selected channel. Check the exact official release tag, or leave it blank for the latest release.'],
    [/runtime release.*(?:HTTP (?:403|429))|runtime.*github.*HTTP (?:403|429)/i, 'GitHub 限制了组件更新请求。请稍后重试；下载网站的 Cookie 不能解决此问题。', 'GitHub has limited runtime update requests. Retry later; source-site cookies do not resolve this issue.'],
    [/runtime release lacks a trusted checksum/i, '此版本缺少可信的官方校验值，已停止安装。请选择其他官方版本。', 'Installation stopped because this release has no trusted official checksum. Choose another official release.'],
    [/incompatible with required application options/i, '此 yt-dlp 版本不支持应用所需参数，已保留原组件。请选择更新的官方版本。', 'This yt-dlp release does not support the required options. The installed component was preserved; choose a newer official release.'],
    [/checksum mismatch/i, '工具或下载包完整性校验失败，已停止执行。请重新下载组件；旧任务需要恢复其原工具版本，或重新添加链接创建新任务。', 'Integrity verification failed and execution stopped. Download the component again; existing tasks need their original tools restored, or add the link as a new task.'],
    [/runtime.*(?:github|download failed)|connect to github/i, '组件更新需要连接 GitHub，不读取“下载代理”设置。请检查 GitHub 网络连接后重试。', 'Runtime updates require access to GitHub and do not use the download proxy setting. Check GitHub connectivity and retry.'],
    [/HTTP(?: Error)? 429|too many requests/i, '来源网站限制了当前出口的请求。请稍后重试；可在网络设置中增大请求间隔后重新添加链接。', 'The source site has limited requests from this connection. Retry later, or increase the request delay in network settings and add the link again.'],
    [/confirm.*bot|sign[ -]?in|logged[ -]in|login required|authentication required/i, '来源网站要求登录或验证。请在“网络与 Cookie”中导入或读取已登录的 Cookie，保存后重新添加链接；已有任务的身份配置保持不变。', 'The source site requires sign-in or verification. Import or read signed-in cookies under Network, save, then add the link again; existing tasks keep their original credentials.'],
    [/HTTP(?: Error)? 403/i, '来源网站拒绝访问。请检查访问权限、Cookie 和下载代理；修改身份设置后需重新添加链接。', 'The source site refused access. Check permissions, cookies, and the download proxy; add the link again after changing credentials.'],
    [/Selected format .*no longer available|No (?:downloadable|video|audio).*format|No audio stream|Requested subtitles.*unavailable/i, '当前视频没有符合方案的格式或字幕。请重新解析，并调整画质、格式或字幕选择。', 'The video has no matching format or subtitles. Read its formats again and adjust quality, format, or subtitle choices.'],
    [/no such option|unrecognized (?:option|argument)/i, '当前工具不支持应用所需参数。请更新运行时组件后重新添加链接；Docker 需更新镜像。', 'The tool does not support the required options. Update runtime components and add the link again; Docker installations require an updated image.'],
    [/is not installed|Cannot start (?:yt-dlp|ffmpeg|ffprobe|deno)/i, '运行时组件缺失或无法启动。请在“工具与更新”检查或重新安装组件；Docker 需检查镜像内组件。', 'A runtime component is missing or cannot start. Check or reinstall it under Tools & updates; Docker installations must check the image components.'],
    [/timed? ?out|timeout|connection refused|unable to download webpage/i, '连接来源网站失败。请检查网络和下载代理后重试。', 'Connection to the source site failed. Check the network and download proxy, then retry.'],
  ];
  const hint = hints.find(([pattern]) => pattern.test(message));
  return hint ? `${t(hint[1], hint[2])} ${t('详细错误：', 'Details: ')}${message}` : message;
}
