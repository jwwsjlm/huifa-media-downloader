# 汇发媒体下载器

汇发媒体下载器是一个 Rust 媒体下载与处理工具：桌面端使用 Tauri，Docker/NAS 使用同一套 React 网页，下载由官方 yt-dlp 驱动，FFmpeg 负责合并、转码和封面处理。它适合保存视频、音频、合集和批量链接，也可以把 yt-dlp 的资料查询与下载队列开放给自己的 AI 客户端。

[English](README_EN.md) · [下载发行版](https://github.com/jwwsjlm/huifa-media-downloader/releases/latest) · [问题反馈](https://github.com/jwwsjlm/huifa-media-downloader/issues)

![Docker 下载任务界面](docs/images/docker-downloads.jpg)

## 功能

- 使用官方 yt-dlp、FFmpeg、FFprobe 和轻量 QuickJS-NG，支持单个视频、音频、合集、批量链接和多个站点；旧任务仍可使用冻结的 Deno 运行时。
- 支持最高/最低画质自动选择、格式和音轨、字幕、章节、片段裁切、SponsorBlock、转码和音频导出。
- 下载与本地处理分开排队，提交时冻结处理计划、工具版本和输入；支持暂停、重启恢复、失败后离线重试。
- 已完成媒体管理、JPEG 封面裁剪与留白、基于原图的 AI 封面编辑。
- Cookie、代理和 AI 密钥以普通本地文件保存；桌面支持内置浏览器和本机兼容浏览器登录取 Cookie，也可读取已有浏览器 Cookie；Docker 提供一次性远程浏览器登录。
- 内置 MCP 服务：AI 可以查询视频资料、格式、合集、字幕、评论，并创建或管理下载任务。服务默认关闭，开启后页面会显示可直接复制的 JSON 配置。
- 中文/英文、深浅色主题、虚拟任务列表和窄窗口布局；发行版提供 SHA-256 校验与签名桌面更新。

## 界面预览

这些截图来自实际运行的 Docker 容器（独立测试数据），不是静态样机。

| 下载队列 | 已完成媒体 |
| --- | --- |
| ![Docker 下载队列](docs/images/docker-downloads.jpg) | ![Docker 已完成媒体](docs/images/docker-completed.jpg) |

| 设置 | AI 媒体服务（MCP） |
| --- | --- |
| ![Docker 设置](docs/images/docker-settings.jpg) | ![Docker MCP 页面](docs/images/mcp-docker.jpg) |

## 安装

在 [Releases](https://github.com/jwwsjlm/huifa-media-downloader/releases/latest) 选择对应文件：

| 平台 | 文件 |
| --- | --- |
| Windows 10/11 x64 安装版 | `HuifaDownloader-<版本>-windows-x64-setup.exe` |
| Windows x64 便携版 | `HuifaDownloader-<版本>-windows-x64-portable.zip` |
| Linux x64 | `HuifaDownloader-<版本>-linux-x64.AppImage` |
| Docker amd64 / ARM64 | `ghcr.io/jwwsjlm/huifa-media-downloader:latest` |

安装包和便携 ZIP 提供同一套程序，均包含下载与处理工具，用户无需另行安装 Python 或 FFmpeg。Windows 安装包目前没有 Authenticode 发布者证书，但安装包、便携 ZIP 和更新清单均有签名及 SHA-256 校验。

便携版解压后运行 `HuifaDownloader.exe`，保留 `tools` 目录和 `portable.flag`。Windows 需要 Microsoft Edge WebView2 Runtime。安装版和便携版共用系统用户数据与下载目录，切换前关闭另一版本；更新便携版时关闭程序并覆盖程序目录。

应用标识为 `com.huifa.downloader.rust`。Rust 数据位于系统本地数据目录的 `HuifaDownloaderRust`，默认下载到系统 Downloads 下的同名目录。Cookie、代理和 AI 密钥在 `credentials` 中以明文文件保存，备份整个数据目录即可。旧版加密记录首次启动时自动迁移，完成后删除旧密钥；迁移前须保留原 `.credentials.key`。

在桌面“设置 → 网络与 Cookie”直接输入来源网站地址，点击“打开内置浏览器”或“本机浏览器登录”，无需经过登录设置弹窗。可选择内置浏览器（Windows WebView2 / Linux WebKit），或已安装的 Edge、Chrome、Chromium 本机浏览器登录窗口。上次成功使用的网址和方式自动保存。登录期间 Cookie 自动同步到本地，关闭浏览器窗口前会再同步一次，无需手动保存。新下载和解析默认按目标网站域名匹配 Cookie，通过 yt-dlp 的 Cookie 文件使用这份身份；已有任务保留原快照。Google 等拒绝内嵌登录的网站可使用本机浏览器。

内置浏览器顶部提供地址栏、返回、前进和刷新；输入网址后按回车访问，未填写协议时使用 HTTPS。导航栏与网站页面隔离，网站及登录弹窗不能调用应用命令。

Cookie 不是所有网站的下载前提：没有匹配记录时匿名解析和下载。同步失败会提示原因，仍允许关闭登录窗口，并保留此前保存的 Cookie。抖音旧提取器可能将接口 403 统一报为 `Fresh cookies (not necessarily logged in)`，反复登录不一定有效。本应用临时内置 [yt-dlp 抖音修复 PR #17826](https://github.com/yt-dlp/yt-dlp/pull/17826)（提交 `edf5ca18`，Unlicense），仅加载随应用编译的兼容代码，不加载用户插件目录；捆绑的官方 yt-dlp 文件不作修改。已经通过公开抖音短链的匿名解析、实际下载和应用入库验证；站点变更仍可能导致失效，上游正式合入后会移除此兼容补丁。

网站登录区只显示自动保存状态，不再列出 Cookie 记录、账号名称或来源选择。登录一次后，新下载按目标网站自动匹配；需要手动导入时，可展开“其他导入方式”。

选择自动画质后，已知单视频链接直接进入队列，包括带播放列表参数的 YouTube 视频。未知链接先识别类型，只有真正的合集才显示项目选择；手动格式只显示格式选择。“剪贴板下载”可读取、去重并直接添加链接。每个任务的分片并发默认 4，推荐 6，上限 8；已有合法配置保留。字幕默认关闭，可从语言下拉开启，人工优先、自动字幕后备，默认外挂 SRT；缺少字幕会提示并继续视频下载。AI 封面设置支持获取模型列表，也可手动填写模型 ID。

新下载保存到“下载目录／视频标题／文件名”，不再创建短任务编号子目录。重复添加同一来源时，正在排队、下载、处理或已暂停的任务会提示并定位原记录；不会重复启动。已完成、失败或取消的任务再次添加时复用原记录，采用本次设置重新下载，新结果验证成功后才替换旧结果；失败或取消会保留旧文件。历史目录不批量迁移，旧任务重新下载成功后才清理被替换的受管文件和空编号目录。

任务阶段前的“2/6”等编号表示当前主要工序和总工序，具体数量按下载、附件或本地转换的实际流程确定。右键及详情可复制视频名和来源；桌面端结果文件存在时可复制包含文件名的完整路径。“转换视频格式”先选择 MP4、MKV、MOV 或 WebM，自动优先直接换封装，必要时重编码；高级选项可手动指定方式和编码器，原始文件保留。侧栏版本号显示新版本提示，点击可打开发行页面，“关于”中可手动检查；便携版不自动运行安装器。

已完成任务和媒体库显示实际画质与文件大小。详情分别展示已下载来源与最终结果的分辨率、帧率、编码和大小，画质按实际媒体信息标记；“最高画质”等下载方案不代表来源一定提供 4K。旧任务优先复用本地记录，缺少结果信息时检测已有文件，不重新下载视频。

删除确认中的“同时删除此任务管理的媒体文件”自动记住上次勾选，取消删除也会保留该选择；新安装默认不勾选，删除仍须确认。设置页的下载参数、语言、主题、通知、代理和 AI 配置点击“保存设置”后写入磁盘；快速选项点击“保存批次方案”后保存。MCP 自启动与服务配置、访问密码、Cookie 和浏览器偏好在各自操作成功后保存。首页临时画质、列表筛选、工具安装参数、Cookie 导入来源及封面编辑中的未提交操作不会作为全局设置自动保存。

登录状态保留在应用数据目录的 `login-browser` 独立配置中，登录浏览器使用系统网络设置。每种浏览器配置可登录多个网站，保存时导出该配置内的 Cookie；删除已保存 Cookie 不会退出浏览器账号。需要退出时在网站中操作。浏览器配置包含登录信息，请按敏感数据保管。

Windows 原生回归可运行 `node scripts/check-login-browser.mjs`（需 WebView2 和 Edge / Chrome）。测试使用独立临时配置与本机测试网站，检查 Cookie 范围、登录弹窗、重开持久化和 IPC 隔离，不使用个人浏览器配置或真实账号。

## Docker 网页

新建一个目录，将以下内容保存为 `compose.yaml`，无需克隆仓库：

```yaml
services:
  downloader:
    image: ghcr.io/jwwsjlm/huifa-media-downloader:latest
    ports:
      - "19491:19491"
    environment:
      HUIFA_ACCOUNT: admin
      HUIFA_PASSWORD: admin
      HUIFA_PUBLIC_ORIGIN: http://localhost:19491
    volumes:
      - ./data:/data
      - ./downloads:/downloads
    read_only: true
    tmpfs:
      - /tmp:rw,exec,nosuid,nodev,size=512m
    shm_size: 256m
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    stop_grace_period: 30s
    restart: unless-stopped
```

`./data` 和 `./downloads` 相对于 `compose.yaml` 所在目录。启动前在该目录创建它们：

```sh
mkdir -p data downloads
```

Linux／NAS 上容器使用 UID/GID `10001:10001`，这两个目录必须允许该用户写入。新建目录可执行以下命令；已有目录应按现有权限配置处理，避免递归修改用户数据。Windows Docker Desktop 无需执行 `chown`。

```sh
sudo chown 10001:10001 data downloads
```

然后启动：

```sh
docker compose pull
docker compose up -d
```

示例使用 `latest`，跟随最新正式版本。以后更新仍在该目录执行以上两条命令，保留 `./data` 和 `./downloads`。

本机打开 [http://localhost:19491](http://localhost:19491)。NAS 或远程访问时，将 `HUIFA_PUBLIC_ORIGIN` 改成浏览器实际访问的完整来源（协议、主机和端口），例如 `http://192.168.1.10:19491`；HTTPS 反向代理使用 `https://download.example.com`，不带路径。修改宿主机端口时，也要同步修改此地址。容器同时支持 amd64 和 ARM64，会自动选择对应架构。

首次安装默认账号和密码均为 `admin`。登录后在“设置 → 访问密码”输入当前密码和新密码即可修改；该密码同时用于网页登录和 HTTP MCP 的 Bearer 鉴权。修改后旧网页会话和 MCP 会话失效，重新登录并更新 AI 客户端密码。桌面端不增加登录页面，也可在相同设置处修改 MCP 访问密码。`HUIFA_ACCOUNT`、`HUIFA_PASSWORD` 仅在数据目录首次初始化时生效；之后以数据目录 `access.json` 中的密码哈希为准，重启或重建容器不会覆盖已修改的密码。请保留 `data` 目录，并在向其他设备开放前修改默认密码。

`data` 保存配置、Cookie 和访问密码，`downloads` 保存媒体；备份时先停止容器，再备份这两个目录及 `compose.yaml`。升级时在原 Compose 目录修改 `image` 的版本号，再执行上面的 `pull`、`up -d`，保留两个本地目录及挂载路径即可沿用数据，无需先删除容器或数据。

已有 `rust-data`／`rust-downloads` 命名卷的用户请先保留原挂载配置，或将数据迁移到本地目录后再切换；修改挂载不会自动迁移旧卷。不要运行 `docker compose down -v`，它会删除配置中引用的命名卷。容器安全、远程浏览器登录、目录权限及多架构构建见 [Docker 文档](docker/README.md)。

## MCP：让自己的 AI 使用 yt-dlp

登录后进入左侧「AI 媒体服务（MCP）」，打开一个开关即可启用全部资料查询和下载管理工具。桌面端默认监听 `127.0.0.1:19492/mcp`；Docker 管理页是 `/mcp`，AI 协议地址是同一来源的 `/mcp-server`，例如：

```json
{
  "mcpServers": {
    "huifa": {
      "url": "http://localhost:19491/mcp-server",
      "headers": { "Authorization": "Bearer YOUR_ACCESS_PASSWORD" }
    }
  }
}
```

页面会根据当前地址生成 JSON；将 `YOUR_ACCESS_PASSWORD` 替换为访问密码（初始为 `admin`）。开启服务即时生效；“软件运行时自动开启 MCP 服务”独立控制下次启动行为，新安装默认关闭，旧配置沿用原有启动状态。管理页的开关操作仍受正常网页登录和 CSRF 保护。MCP 继承软件保存的 Cookie、代理和下载设置，只返回清理后的资料、任务状态和错误，不返回 Cookie、代理密码、内部目录或带签名的媒体直链，因此只应把地址和密码交给信任的 AI 客户端。桌面还提供 stdio 桥接配置，使用安装包自带的 `huifa-mcp-bridge`，通过 `HUIFA_MCP_PASSWORD` 设置相同密码，不需要 Python 或 Node。

工具与完整参数见 [MCP 使用说明](docs/MCP.md)。

## 开发与发布

需要 Node.js 24、Rust stable 和对应平台的 Tauri 编译依赖：

```sh
npm ci --prefix frontend
node scripts/acquire-rust-tools.mjs
npm --prefix frontend run tauri -- dev
```

构建、验收、MCP 桥接程序准备和 GitHub 发布流程见 [开发与发布文档](docs/DEVELOPMENT.md)。当前源码、检查和发布均以 Rust 实现为准。
