# 汇发媒体下载器

下载视频、音频和合集，管理本地媒体，并进行格式转换、视频压缩与封面处理。Windows、macOS、Linux 桌面版和 Docker/NAS 网页版共用一套界面，下载由官方 yt-dlp 驱动，媒体处理使用 FFmpeg。

[English](README_EN.md) · [下载最新版](https://github.com/jwwsjlm/huifa-media-downloader/releases/latest) · [使用指南](docs/USAGE.md) · [问题反馈](https://github.com/jwwsjlm/huifa-media-downloader/issues)

![下载任务界面](docs/images/docker-downloads.jpg)

## 下载与安装

在[最新发行版](https://github.com/jwwsjlm/huifa-media-downloader/releases/latest)选择对应系统和处理器的文件。桌面包已包含下载、媒体处理和 JavaScript 运行时，无需另装 Python 或 FFmpeg。

| 系统 | 选择的文件 |
| --- | --- |
| Windows 10/11 x64 | `*-windows-x64-setup.exe` 或 `*-windows-x64-portable.zip` |
| Windows ARM64 | `*-windows-arm64-setup.exe` 或 `*-windows-arm64-portable.zip` |
| Linux x64 | `*-linux-x64.AppImage` |
| Linux ARM64 | `*-linux-arm64.AppImage` |
| Mac Intel，macOS 15+ | `*-macos-x64.dmg` |
| Mac Apple 芯片，macOS 15+ | `*-macos-arm64.dmg` |

- **Windows**：需要 WebView2 Runtime。便携版解压后运行 `HuifaDownloader.exe`，保留同目录的 `tools` 和 `portable.flag`；安装版与便携版共用用户数据。安装包目前没有 Authenticode 发布者证书。
- **Linux**：为 AppImage 添加执行权限后运行，例如 `chmod +x ./HuifaDownloader-*-linux-x64.AppImage`；`aarch64` 处理器选择 ARM64。
- **macOS**：打开 DMG，将应用拖到“应用程序”。当前使用 ad-hoc 签名，尚未经 Apple 公证；首次打开被拦截时，确认下载来源后按 [Apple 官方说明](https://support.apple.com/zh-cn/102445)操作。`.app.tar.gz` 是应用更新包。

发行页提供 `SHA256SUMS` 和桌面更新签名。Docker/NAS 安装见下方 [Docker 部署](#docker-部署)。

## 开始使用

1. 粘贴一个或多个视频链接，选择画质后加入队列。自动画质可直接下载单个视频，合集和手动格式会先显示选择界面。
2. 需要登录的网站，在“设置 → 网络与 Cookie”打开浏览器登录；新任务会按网站自动匹配 Cookie。公开内容可以匿名下载。
3. 下载完成后，在任务菜单中定位或下载文件、转换格式、压缩视频或查看日志；在已完成详情中打开媒体或制作封面。

下载目录在“设置 → 下载与处理”中选择，仅影响新任务。Cookie、代理和 AI 密钥以明文文件保存在应用数据目录，请按敏感数据保管。操作与备份细节见 [使用指南](docs/USAGE.md)。

## 主要功能

- **下载与批量**：视频、音频、合集和批量链接；支持画质、音轨、字幕、章节、片段裁切和 SponsorBlock，具体能力以来源网站为准。
- **队列与恢复**：下载和本地处理分开排队，支持暂停、优先执行、重启恢复、有限次数的断流重试，以及处理失败后复用本地来源重试。
- **视频压缩**：简单模式按目标大小压缩，高级模式调整码率、缩小分辨率或降低帧率；默认 HEVC/MKV，另存新文件并保留原视频。
- **GPU 编码加速**：针对当前视频实际检测可用编码器，不支持时说明原因；失败后可选择 CPU 重试。无法保留轨道、位深或 HDR 的组合会明确拒绝。
- **组件版本选择**：FFmpeg 默认“兼容优先”，也可选择“效率优先”或指定官方版本标签；新任务使用当前组件，已有任务保留原工具。
- **媒体管理**：显示实际画质、大小和本地文件状态；支持格式转换、音频导出、JPEG 封面裁剪与留白，以及基于原图的 AI 封面编辑。
- **桌面体验**：中英文、深浅主题、批量选择和可选的剪贴板链接自动下载；剪贴板监控默认关闭。
- **AI 客户端接入**：内置 MCP，允许自己的 AI 查询视频资料、格式、字幕和评论，并操作下载队列。

压缩是有损处理，过小的目标可能无法容纳完整视频和保留轨道。macOS 随包的 CPU x265 目前只支持 8 位；不支持的 10 位/HDR 编码路径会禁用。详见 [压缩与格式转换](docs/USAGE.md#压缩与格式转换)。

## Docker 部署

镜像：`ghcr.io/jwwsjlm/huifa-media-downloader:latest`，支持 **amd64 / ARM64**，无需克隆仓库。

按 [Docker 部署文档](docker/README.md#released-image)保存 `compose.yaml`、准备数据目录并启动，然后访问 `http://localhost:19491`。首次账号和密码为 `admin/admin`，向其他设备开放前请修改密码；NAS 或反向代理需要将 `HUIFA_PUBLIC_ORIGIN` 设为浏览器实际访问的来源。

`data` 保存配置和登录信息，`downloads` 保存媒体。更新时保留这两个目录；容器组件随镜像更新。

## 连接自己的 AI 客户端

1. 打开“AI 媒体服务（MCP）”并启用服务，默认关闭。
2. 复制页面生成的 HTTP、stdio 或 Codex 配置，将密码占位符替换为访问密码。
3. 在自己的 AI 客户端中添加配置，并保持应用或容器运行。

桌面默认地址为 `http://127.0.0.1:19492/mcp`；Docker 管理页是 `/mcp`，AI 连接地址是同一来源的 `/mcp-server`。只把地址和密码交给信任的客户端。完整配置和工具列表见 [MCP 使用说明](docs/MCP.md)。

## 文档导航

| 文档 | 内容 |
| --- | --- |
| [使用指南](docs/USAGE.md) | 下载选项、压缩、任务恢复、登录、文件安全和故障排查 |
| [组件更新](docs/RUNTIME_UPDATES.md) | yt-dlp 通道、FFmpeg 版本策略、下载校验与任务工具快照 |
| [Docker 部署](docker/README.md) | Compose、NAS 权限、反向代理、数据保留和远程浏览器 |
| [MCP 使用说明](docs/MCP.md) | AI 客户端配置、工具参数和权限边界 |
| [开发与发布](docs/DEVELOPMENT.md) | 本地开发、测试、构建和 GitHub Actions 发布 |
| [版本记录](https://github.com/jwwsjlm/huifa-media-downloader/releases) | 每个发行版的变更和下载文件 |

## 本地开发

需要 Node.js 24、Rust stable 和对应平台的 Tauri 编译依赖。在仓库根目录执行：

```sh
npm ci --prefix frontend
node scripts/acquire-rust-tools.mjs
npm --prefix frontend run tauri -- dev
```

构建和验收命令见 [开发与发布](docs/DEVELOPMENT.md)。

<details>
<summary>更多界面截图</summary>

以下截图来自使用独立测试数据的实际 Docker 容器。

| 已完成媒体 | 设置 |
| --- | --- |
| ![已完成媒体](docs/images/docker-completed.jpg) | ![设置](docs/images/docker-settings.jpg) |

![AI 媒体服务（MCP）](docs/images/mcp-docker.jpg)

</details>
