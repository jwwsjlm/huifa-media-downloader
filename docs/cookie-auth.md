# Cookie 登录与复用

## yt-dlp 依赖

requirements.txt 使用 yt-dlp[default,curl-cffi]，由 yt-dlp 自己拉取并维护 yt-dlp-ejs、certifi、brotli、mutagen、pycryptodomex、websockets 和 curl_cffi。应用使用 yt-dlp/FFmpeg-Builds 兼容的 FFmpeg/FFprobe，并明确优先使用 Deno 作为 yt-dlp-ejs JavaScript runtime，不把本机 Node.js 作为默认运行时。

## 设计

程序按需加载 PySide6 QtWebEngine/Chromium。下载侧支持：

- `none`：不携带 Cookie；
- `embedded`：使用内置浏览器登录，Cookie 主副本通过当前 Windows 用户的 DPAPI 加密保存在 `data/browser/cookies`；
- `file`：读取用户选择的 Netscape Cookie 文件；
- `browser`：通过 yt-dlp 的 `cookiesfrombrowser` 直接读取当前 Windows 用户的 Chrome、Edge、Firefox 或 Brave Cookie 数据库。

外部浏览器 Cookie 的解密、临时数据库复制和 DPAPI/NSS 兼容仍由 yt-dlp 负责。内置 Cookie 由应用使用 DPAPI 加密，下载任务启动时生成临时 Netscape 文件，任务结束立即删除。Cookie 值不会写入 SQLite、settings.ini、任务日志或诊断包。

## 登录流程

1. 在设置页选择“内置浏览器 Cookie（推荐）”。
2. 点击“打开登录页”，在内置 QtWebEngine 窗口完成登录；也可导入 Netscape、Playwright `storage_state` 或粘贴 Cookie Header。
3. 点击“完成并保存”，Cookie 使用 DPAPI 加密落盘。
4. 点击“检查 Cookie”只返回数量/可读性，不显示 Cookie 名称或值。
5. 新下载任务只在运行期间把该会话转换为临时 Netscape 文件交给 yt-dlp。

内置浏览器会持续监听 Cookie 的新增、更新和删除，并在短暂防抖后自动写入 DPAPI 加密仓库；关闭浏览器窗口、退出程序时还会再次强制保存。因此会话 Cookie 也能在软件重启后恢复，不要求每次重新登录。“查看 Cookie”按域名分组显示名称、路径、有效期、Secure、HttpOnly 和 SameSite，支持搜索与刷新；Cookie 值默认遮罩，只有用户明确勾选后才临时显示，且不会进入日志或诊断包。

“打开登录页”始终使用程序内置 QtWebEngine/Chromium，并自动切换到内置 Cookie 来源，不会启动系统默认浏览器。为兼容已有配置，仍可手动选择“读取已有浏览器 Cookie（高级）”读取既有 Chrome、Edge、Firefox 或 Brave Profile，但这只是高级导入来源，不参与程序内置登录流程。

## 发布侧

账号中心为每个平台/账号创建独立的内置浏览器 profile；登录按钮和“平台创作后台”入口都复用该 profile，不调用系统浏览器。QtWebEngine 初始化前会启用仅监听 `127.0.0.1` 随机端口的 CDP。完成登录后，程序先保存 DPAPI 加密主副本，再在后台调用 SAU 安装环境自己的 Patchright/Playwright，通过 CDP 直接导出同一浏览器 context 的完整 `storage_state`（Cookie 与 origin/localStorage）到 `cookies/<platform>_<account>.json`，随后调用 `sau ... check` 验证。找不到 SAU 对应 Python 运行时或 CDP 不兼容时，会自动回退为 Cookie-only `storage_state`。SAU 要求的 JSON 本身是明文兼容文件；可在设置中明确指定其 Cookie 目录。Bilibili 使用 biliup 专用账号文件，因此账号写入仍使用原来的交互终端流程，但其创作后台网页同样在内置 Chromium 中打开。

## 兼容性

已有只配置 `download_cookie_file` 的安装会自动按 Netscape 文件来源继续使用；关闭浏览器后重试可降低浏览器数据库锁定导致的读取失败。
