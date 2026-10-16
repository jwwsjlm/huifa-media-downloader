# 汇发媒体下载器

汇发媒体下载器是一个 Rust 媒体下载与处理工具：桌面端使用 Tauri，Docker/NAS 使用同一套 React 网页，下载由官方 yt-dlp 驱动，FFmpeg 负责合并、转码和封面处理。它适合保存视频、音频、合集和批量链接，也可以把 yt-dlp 的资料查询与下载队列开放给自己的 AI 客户端。

[English](README_EN.md) · [下载发行版](https://github.com/jwwsjlm/huifa-media-downloader/releases/latest) · [问题反馈](https://github.com/jwwsjlm/huifa-media-downloader/issues)

![Docker 下载任务界面](docs/images/docker-downloads.jpg)

## 功能

- 使用官方 yt-dlp、FFmpeg、FFprobe 和轻量 QuickJS-NG，支持单个视频、音频、合集、批量链接和多个站点；旧任务仍可使用冻结的 Deno 运行时。
- 支持最高/最低画质自动选择、格式和音轨、字幕、章节、片段裁切、SponsorBlock、转码和音频导出。
- 下载与本地处理分开排队，提交时冻结处理计划、工具版本和输入；支持暂停、重启恢复、失败后离线重试。
- 已完成媒体管理、JPEG 封面裁剪与留白、基于原图的 AI 封面编辑。
- Cookie、代理和 AI 密钥加密保存；桌面可读取浏览器 Cookie，Docker 可通过一次性远程浏览器登录。
- 内置 MCP 服务：AI 可以查询视频资料、格式、合集、字幕、评论，并创建或管理下载任务。服务默认关闭，开启后页面会显示可直接复制的 JSON 配置。
- 中文/英文、深浅色主题、虚拟任务列表和窄窗口布局；发行版提供 SHA-256 校验与签名桌面更新。

## 界面预览

这些截图来自实际运行的 Docker 容器（独立测试数据），不是静态样机。

| 下载队列 | 已完成媒体 |
| --- | --- |
| ![Docker 下载队列](docs/images/docker-downloads.jpg) | ![Docker 已完成媒体](docs/images/docker-completed.jpg) |

| 设置 | AI 视频服务（MCP） |
| --- | --- |
| ![Docker 设置](docs/images/docker-settings.jpg) | ![Docker MCP 页面](docs/images/mcp-docker.jpg) |

## 安装

在 [Releases](https://github.com/jwwsjlm/huifa-media-downloader/releases/latest) 选择对应文件：

| 平台 | 文件 |
| --- | --- |
| Windows 10/11 x64 安装版 | `HuifaDownloader-<版本>-windows-x64-setup.exe` |
| Windows x64 便携版 | `HuifaDownloader-<版本>-windows-x64-portable.zip` |
| Linux x64 | `HuifaDownloader-<版本>-linux-x64.AppImage` |
| Docker amd64 / ARM64 | `ghcr.io/jwwsjlm/huifa-media-downloader:<版本>` |

安装包和便携 ZIP 提供同一套程序，均包含下载与处理工具，用户无需另行安装 Python 或 FFmpeg。Windows 安装包目前没有 Authenticode 发布者证书，但安装包、便携 ZIP 和更新清单均有签名及 SHA-256 校验。

便携版解压后运行 `HuifaDownloader.exe`，保留 `tools` 目录和 `portable.flag`。Windows 需要 Microsoft Edge WebView2 Runtime。安装版和便携版共用系统用户数据与下载目录，切换前关闭另一版本；更新便携版时关闭程序并覆盖程序目录。

应用标识为 `com.huifa.downloader.rust`。Rust 数据位于系统本地数据目录的 `HuifaDownloaderRust`，默认下载到系统 Downloads 下的同名目录。备份时请同时保留 `.credentials.key`，否则无法恢复加密 Cookie 和 AI 密钥。

## Docker 网页

在仓库根目录创建 `.env`：

```dotenv
HUIFA_IMAGE=ghcr.io/jwwsjlm/huifa-media-downloader:0.3.0
HUIFA_ACCOUNT=your-account
HUIFA_PASSWORD=your-password
HUIFA_PUBLIC_ORIGIN=http://localhost:19491
```

```sh
docker compose up -d --no-build
```

打开 `HUIFA_PUBLIC_ORIGIN` 登录。远程访问时把它改成浏览器实际使用的完整来源。`rust-data` 和 `rust-downloads` 两个卷分别保存配置与媒体；不要用 `docker compose down -v` 删除需要保留的数据。完整的容器安全、浏览器登录和多架构构建说明见 [Docker 文档](docker/README.md)。

## MCP：让自己的 AI 使用 yt-dlp

登录后进入左侧「AI 视频服务（MCP）」，打开一个开关即可启用全部资料查询和下载管理工具。桌面端默认监听 `127.0.0.1:19492/mcp`；Docker 管理页是 `/mcp`，AI 协议地址是同一来源的 `/mcp-server`，例如：

```json
{
  "mcpServers": {
    "huifa": {
      "url": "http://localhost:19491/mcp-server"
    }
  }
}
```

页面会根据当前地址生成 JSON。MCP 调用不使用令牌；管理页的开关操作仍受正常网页登录和 CSRF 保护。MCP 继承软件保存的 Cookie、代理和下载设置，只返回清理后的资料、任务状态和错误，不返回 Cookie、代理密码、内部目录或带签名的媒体直链，因此只应把地址交给信任的 AI 客户端。桌面还提供 stdio 桥接配置，使用安装包自带的 `huifa-mcp-bridge`，不需要 Python 或 Node。

工具与完整参数见 [MCP 使用说明](docs/MCP.md)。

## 开发与发布

需要 Node.js 24、Rust stable 和对应平台的 Tauri 编译依赖：

```sh
npm ci --prefix frontend
node scripts/acquire-rust-tools.mjs
npm --prefix frontend run tauri -- dev
```

构建、验收、MCP 桥接程序准备和 GitHub 发布流程见 [开发与发布文档](docs/DEVELOPMENT.md)。当前源码、检查和发布均以 Rust 实现为准。
