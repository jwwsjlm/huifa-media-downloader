# 汇发媒体下载器

Rust 下载与媒体处理核心，Tauri 桌面应用与 Docker 网页共用 React 界面。

[English](README_EN.md) · [下载发行版](https://github.com/jwwsjlm/huifa-media-downloader/releases/latest) · [问题反馈](https://github.com/jwwsjlm/huifa-media-downloader/issues)

![下载任务界面](docs/images/download-tasks.jpg)

## 功能

- 官方 yt-dlp、FFmpeg、FFprobe 和 Deno，支持单个视频、音频、合集和批量链接。
- 下载与本地处理独立排队：普通下载最多 3 项并行，特殊或直播任务 1 项，媒体处理 1 项。
- 格式与清晰度选择、多音轨、字幕、章节、片段裁切、SponsorBlock、转码和音频导出。
- 提交时固定处理计划和工具版本；保留原始输入，支持暂停、重启恢复和处理失败后的离线重试。
- 已完成媒体管理、JPEG 封面裁剪与留白、基于原图的 AI 封面编辑。
- Cookie 和 AI 密钥加密存储；桌面支持读取浏览器 Cookie，Docker 提供远程浏览器登录。
- 深浅色主题、中文与英文界面、虚拟任务列表和窄窗口布局。
- GitHub Actions 统一构建发行包，提供 SHA-256 校验和签名桌面更新。

## 下载和安装

在 [Releases](https://github.com/jwwsjlm/huifa-media-downloader/releases/latest) 选择对应文件：

| 平台 | 文件 |
| --- | --- |
| Windows 10/11 x64 | `HuifaDownloader-<版本>-windows-x64-setup.exe` |
| Windows x64 便携版 | `HuifaDownloader-<版本>-windows-x64-portable.zip` |
| Linux x64 | `HuifaDownloader-<版本>-linux-x64.AppImage` |
| Docker amd64 / ARM64 | `ghcr.io/jwwsjlm/huifa-media-downloader:<版本>` |

Windows 运行安装包；Linux 给 AppImage 添加执行权限后运行。应用包含所需下载和处理工具，用户无需另行安装 Python 或 FFmpeg。更新签名用于验证更新来源，Windows 安装包目前没有 Authenticode 发布者证书。

便携版解压后运行 `HuifaDownloader.exe`，保留旁边的 `tools` 目录和 `portable.flag`。Windows 需要已安装 Microsoft Edge WebView2 Runtime。便携版与安装版共用下述系统用户数据和下载目录，切换前关闭另一版本；更新时关闭程序，再用新的便携 ZIP 覆盖程序目录。便携版不执行安装包自动更新，ZIP 同样提供 `.sig` 签名及 SHA-256 校验。

新应用标识为 `com.huifa.downloader.rust`，数据保存到系统本地数据目录下的 `HuifaDownloaderRust`，默认下载到系统下载目录下的同名文件夹。旧版本安装和数据保留在原处，需要单独备份。备份 Rust 数据时请同时备份 `.credentials.key`，否则无法恢复已加密的 Cookie 和 AI 密钥。

视频发布及发布账号功能暂时关闭。

## Docker 网页

使用仓库中的 `compose.yaml`，在旁边创建 `.env` 并设置：

```dotenv
HUIFA_IMAGE=ghcr.io/jwwsjlm/huifa-media-downloader:0.2.0
HUIFA_ACCOUNT=your-account
HUIFA_PASSWORD=your-password
HUIFA_PUBLIC_ORIGIN=http://localhost:19491
```

```sh
docker compose up -d --no-build
```

打开配置的地址登录。远程访问时将 origin 改为实际服务器域名或 IP。数据和媒体分别保存在 `rust-data`、`rust-downloads` 数据卷；不要使用 `docker compose down -v` 删除需要保留的数据。

直接 HTTP 访问时登录信息以明文传输，公网部署建议使用 HTTPS 反向代理。容器以非 root 用户运行；远程 Chromium 受容器隔离保护，部分网站可能拒绝无头浏览器登录。完整配置见 [Docker 说明](docker/README.md)。

## 从源码开发与发布

需要 Node.js 24、Rust stable 和对应平台的 Tauri 编译依赖。

```sh
npm ci --prefix frontend
node scripts/acquire-rust-tools.mjs
npm --prefix frontend run tauri -- dev
```

构建、验收与手动版本发布见 [开发与发布文档](docs/DEVELOPMENT.md)，接口约定见 [实现说明](docs/RUST_IMPLEMENTATION.md)。

旧 Python、Qt 和 Electron 实现已从当前代码树移除，可从 Git 基线提交 `bf79d1a` 找回。本机清理将旧文件移入 Windows 回收站。
