# 汇发媒体下载器

汇发媒体下载器是一个 Rust 媒体下载与处理工具：Windows、macOS 和 Linux 桌面端使用 Tauri，Docker/NAS 使用同一套 React 网页，下载由官方 yt-dlp 驱动，FFmpeg 负责合并、转码和封面处理。它适合保存视频、音频、合集和批量链接，也可以把 yt-dlp 的资料查询与下载队列开放给自己的 AI 客户端。

[English](README_EN.md) · [下载发行版](https://github.com/jwwsjlm/huifa-media-downloader/releases/latest) · [问题反馈](https://github.com/jwwsjlm/huifa-media-downloader/issues)

![Docker 下载任务界面](docs/images/docker-downloads.jpg)

## 功能

- 使用官方 yt-dlp、FFmpeg、FFprobe 和对应平台的 JavaScript 运行时，支持单个视频、音频、合集、批量链接和多个站点；旧任务保留冻结的运行时。
- 支持最高/最低画质自动选择、格式和音轨、字幕、章节、片段裁切、SponsorBlock、转码和音频导出。
- 下载与本地处理分开排队，提交时冻结处理计划、工具版本和输入；支持暂停、重启恢复、失败后离线重试。
- 已完成媒体管理、JPEG 封面裁剪与留白、基于原图的 AI 封面编辑。
- Cookie、代理和 AI 密钥以普通本地文件保存；Windows/Linux 支持内置浏览器或本机兼容浏览器登录，macOS 使用 Chrome、Edge 或 Chromium 本机浏览器；也可导入 Cookie，Docker 提供一次性远程浏览器登录。
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

在[最新发行版](https://github.com/jwwsjlm/huifa-media-downloader/releases/latest)下载对应系统和处理器的文件。桌面覆盖 Windows x64/ARM64、Linux x64/ARM64、Mac Intel 和 Apple 芯片六个平台：

| 平台 | 文件 |
| --- | --- |
| Windows 10/11 x64 | `HuifaDownloader-<版本>-windows-x64-setup.exe` 或 `HuifaDownloader-<版本>-windows-x64-portable.zip` |
| Windows ARM64 | `HuifaDownloader-<版本>-windows-arm64-setup.exe` 或 `HuifaDownloader-<版本>-windows-arm64-portable.zip` |
| Linux x64 | `HuifaDownloader-<版本>-linux-x64.AppImage` |
| Linux ARM64 | `HuifaDownloader-<版本>-linux-arm64.AppImage` |
| Mac Intel（macOS 15 及以上） | `HuifaDownloader-<版本>-macos-x64.dmg` |
| Mac Apple 芯片（M 系列，macOS 15 及以上） | `HuifaDownloader-<版本>-macos-arm64.dmg` |
| Docker amd64 / ARM64 | `ghcr.io/jwwsjlm/huifa-media-downloader:latest` |

各桌面包均包含对应架构的下载与处理工具，无需另行安装 Python 或 FFmpeg。发行页提供 `SHA256SUMS`；桌面更新包另有更新签名。Windows 安装包目前没有 Authenticode 发布者证书。

Windows 便携版解压后运行 `HuifaDownloader.exe`，保留 `tools` 目录和 `portable.flag`。Windows 需要 Microsoft Edge WebView2 Runtime。安装版和便携版共用系统用户数据与下载目录，切换前关闭另一版本；更新便携版时关闭程序并覆盖程序目录。

Linux 下载与处理器架构对应的 AppImage，添加执行权限后运行，例如 `chmod +x ./HuifaDownloader-*-linux-x64.AppImage`；ARM64 文件使用 `linux-arm64`。`x86_64` 选 x64，`aarch64` 选 ARM64。

Mac 需要 macOS 15 及以上，与随包 FFmpeg 的系统要求一致。在“关于本机”查看芯片类型：Intel 选 `macos-x64.dmg`，Apple M 系列选 `macos-arm64.dmg`。打开 DMG，将应用拖到“应用程序”后启动；发行页的同架构 `.app.tar.gz` 是应用更新包。当前 Mac 应用使用 ad-hoc 签名，尚未经 Apple 公证。首次打开若提示无法验证开发者，确认文件来自本项目发行页后，可按 [Apple 官方说明](https://support.apple.com/zh-cn/102445)在“系统设置 → 隐私与安全性”选择“仍要打开”。

Windows 启动失败会显示错误及 `startup-error.log` 的位置。如果双击后直接退出且没有提示，可下载 `scripts/diagnose-windows-startup.ps1`，用 Windows PowerShell 运行 `powershell -NoProfile -ExecutionPolicy Bypass -File .\diagnose-windows-startup.ps1`，按提示输入程序 EXE 路径，将生成的 `startup-report.txt` 发给开发者。脚本采集系统、程序和 WebView2 版本、退出码及本次错误事件，不读取 Cookie 或业务配置。

应用标识为 `com.huifa.downloader.rust`。Rust 数据位于系统本地数据目录的 `HuifaDownloaderRust`，默认下载到系统 Downloads 下的同名目录。Cookie、代理和 AI 密钥在 `credentials` 中以明文文件保存，备份整个数据目录即可。旧版加密记录首次启动时自动迁移，完成后删除旧密钥；迁移前须保留原 `.credentials.key`。

在桌面“设置 → 网络与 Cookie”直接输入来源网站地址，无需经过登录设置弹窗。Windows/Linux 可点击“打开内置浏览器”（Windows WebView2 / Linux WebKit）或“本机浏览器登录”；macOS 仅提供“本机浏览器登录”，需要安装 Chrome、Edge 或 Chromium，支持从 `/Applications` 或 `~/Applications` 识别浏览器，Safari 不用于此登录流程。上次成功使用的网址和方式自动保存。登录期间 Cookie 自动同步到本地，关闭浏览器窗口前会再同步一次，无需手动保存。新下载和解析默认按目标网站域名匹配 Cookie，通过 yt-dlp 的 Cookie 文件使用这份身份；已有任务保留原快照。Google 等拒绝内嵌登录的网站可使用本机浏览器。

Windows/Linux 内置浏览器顶部提供地址栏、返回、前进和刷新；输入网址后按回车访问，未填写协议时使用 HTTPS。导航栏与网站页面隔离，网站及登录弹窗不能调用应用命令。桌面登录浏览器与下载共用代理；修改代理后关闭并重新打开登录窗口，保存设置后重启仍生效。登录浏览器需要不含账号密码的代理地址，可使用本地代理端口。

Cookie 不是所有网站的下载前提：没有匹配记录时匿名解析和下载。同步失败会提示原因，仍允许关闭登录窗口，并保留此前保存的 Cookie。抖音旧提取器可能将接口 403 统一报为 `Fresh cookies (not necessarily logged in)`，反复登录不一定有效。本应用临时内置 [yt-dlp 抖音修复 PR #17826](https://github.com/yt-dlp/yt-dlp/pull/17826)（提交 `edf5ca18`，Unlicense），仅加载随应用编译的兼容代码，不加载用户插件目录；捆绑的官方 yt-dlp 文件不作修改。已经通过公开抖音短链的匿名解析、实际下载和应用入库验证；站点变更仍可能导致失效，上游正式合入后会移除此兼容补丁。

网站登录区只显示自动保存状态，不再列出 Cookie 记录、账号名称或来源选择。登录一次后，新下载按目标网站自动匹配；需要手动导入时，可展开“其他导入方式”。

选择自动画质后，已知单视频链接直接进入队列，包括带播放列表参数的 YouTube 视频。未知链接先识别类型，只有真正的合集才显示项目选择；手动格式只显示格式选择。“剪贴板下载”可读取、去重并直接添加链接。每个任务的分片并发默认 4，推荐 6，上限 8；已有合法配置保留。字幕默认关闭，开启后人工优先、自动字幕后备，默认外挂 SRT。明确选择的画质、帧率或音轨不满足时会停止该任务并列出缺项，通过“调整要求重试”由用户放宽；自动最高／最低画质仍自动选择。“无字幕时继续下载”默认开启，缺少所选字幕时跳过缺失语言并继续视频，不弹出要求调整；关闭后才要求字幕必须满足。AI 封面设置支持获取模型列表，也可手动填写模型 ID。

桌面版可在“设置 → 下载与处理 → 下载目录”点击“选择目录”，选定可写文件夹后立即保存，重启后继续使用。该设置只影响新建或重新添加的任务；已有排队、下载、暂停和处理任务保留原目录，已有文件不会搬动。Docker 的下载位置仍通过 Compose 的 `./downloads:/downloads` 映射配置，网页不能选择宿主机本地目录。

新下载保存到“下载目录／视频标题／视频标题.扩展名”，默认文件名不追加来源编号或任务 ID，也不创建短任务编号子目录。旧版默认文件名模板自动更新，历史文件不批量改名；转换结果使用“标题 [converted]”，同名转换用递增序号区分并保留原文件。重复添加同一来源时，正在排队、下载、处理或已暂停的任务会提示并定位原记录；不会重复启动。已完成、失败或取消的任务再次添加时复用原记录，采用本次设置重新下载，新结果验证成功后才替换旧结果；失败或取消会保留旧文件。历史目录不批量迁移，旧任务重新下载成功后才清理被替换的受管文件和空编号目录。

任务阶段前的“2/6”等编号表示当前主要工序和总工序，具体数量按下载、附件或本地转换的实际流程确定，完成后隐藏编号。右键及详情可复制视频名和来源；桌面端结果文件存在时可复制包含文件名的完整路径。“转换视频格式”先选择 MP4、MKV、MOV 或 WebM，自动优先直接换封装，必要时重编码；高级选项可手动指定方式和编码器，原始文件保留。侧栏版本号显示新版本提示，点击可打开发行页面，“关于”中可手动检查；便携版不自动运行安装器。

已完成任务和媒体库显示实际画质与文件大小。详情分别展示已下载来源与最终结果的分辨率、帧率、编码和大小，画质按实际媒体信息标记；“最高画质”等下载方案不代表来源一定提供 4K。旧任务优先复用本地记录，缺少结果信息时检测已有文件，不重新下载视频。

任务列表在“已完成”旁显示本地完成日期与时间，精确到分钟。在“设置 → 外观与封面 → 完成时间格式”自定义 `YYYY-MM-DD HH:mm`、`YYMMDD HH:mm` 等格式，`MM` 为月份，`mm` 为分钟。旧任务只从已有完成日志补回时间，没有记录时留空。

下载中的大小栏显示累计已下载大小；总大小已知时同时显示总量，估算值标记为 `≈`，完成后显示最终结果大小。任务列表进入时默认不选中任务，后台进度更新不会自动选择；支持 Shift 连选、Ctrl 切换选择和 Ctrl+A 全选，也可在列表标题旁全选当前筛选结果并批量删除。批量删除沿用媒体文件删除确认；部分失败时保留失败项供重试。

高帧率结果显示为 `1080p60`、`4K60` 等，`59.94 fps` 在简写中按 `60` 显示，详情保留精确帧率；未读到帧率时不推测。任务列表与媒体库共用封面缩略图风格和右键菜单。失败或取消后显示最终状态，原工序位置仍保存在任务与日志中；底部总下载速度汇总所有正在下载的任务，不受列表筛选影响。

“查看日志”保存 yt-dlp 的原始调用参数、标准输出、错误输出和进度，不脱敏或按条数截断。窗口分页读取并自动刷新，可复制完整日志；旧任务无法补回此前未记录的输出。日志可能含 Cookie、认证信息、本地路径及临时媒体地址，分享前请检查。完整原始日志仅通过已鉴权的任务日志接口读取，不进入普通快照或 MCP 工具结果。

已结束的直播回放按普通视频筛选；正在直播或等待开播的来源仍遵守方案中的直播筛选，“全部内容”允许这两类来源，是否等待开播由独立选项控制。

版本检查优先读取 GitHub 最新 Release 接口；接口限额或连接失败时，改用本项目官方 Release 页的跳转地址确认版本。两条路径均失败时显示具体原因，可手动重试。

删除确认中的“同时删除此任务管理的媒体文件”自动记住上次勾选，取消删除也会保留该选择；新安装默认不勾选，删除仍须确认。设置页的下载参数、语言、主题、通知、代理和 AI 配置点击“保存设置”后写入磁盘；快速选项点击“保存批次方案”后保存。MCP 自启动与服务配置、访问密码、Cookie 和浏览器偏好在各自操作成功后保存。首页临时画质、列表筛选、工具安装参数、Cookie 导入来源及封面编辑中的未提交操作不会作为全局设置自动保存。

登录状态保留在应用数据目录的 `login-browser` 独立配置中，桌面登录浏览器共用保存的下载代理，留空直连。每种浏览器配置可登录多个网站，保存时导出该配置内的 Cookie；删除已保存 Cookie 不会退出浏览器账号。需要退出时在网站中操作。浏览器配置包含登录信息，请按敏感数据保管。

### 批量、恢复与文件安全

- 混合批量按链接独立识别和添加，显示进度并可取消剩余识别；一个坏链接不会中断其它链接。页面保留失败链接，并在本次批量任务结束后汇总异常或待处理项；关闭应用后仍可通过失败分类查看任务。
- 临时超时、断网、429 和服务端错误有限次数退避重试；登录、权限、缺失格式和明确要求不足等待用户处理。普通“继续／重试”沿用冻结方案；“使用当前设置重试”刷新方案、Cookie、代理和工具。同源同格式可复用分片，来源或格式变化会隔离旧分片，避免拼错视频。
- 不同来源同名时，在媒体传输前询问改名、跳过或覆盖；覆盖需要第二次确认。旧结果和受管附件仅在新结果验证成功后替换；失败或取消保留原文件。旧任务结果被替换后会明确标记，不会继续冒充拥有新文件。
- “优先执行”实际调整尚未开始的下载、直播或处理队列，并持久保存优先级；已执行的任务继续运行。
- 直播提供“结束直播并保存”，将已落盘内容验证并处理为正式结果；普通“取消”不发布结果。录制停止瞬间尚未写入的尾部数据可能丢失，暂停后恢复直播也可能存在时间缺口。
- 开始前检查应用数据盘、处理目录和结果盘空间；大小未知时无法预估整段视频的占用。成功后自动清理来源和处理中间文件，保留成品、缩略图预览与日志；暂停、失败任务保留恢复文件。设置中的“磁盘与缓存”显示剩余容量并可清理已完成缓存及没有任务引用的旧组件。
- 非直播来源检查预期与实际时长，合并／转码后再检查来源与结果时长、所需流和已有哈希。它不是对全视频逐帧解码，也不能证明网站提供的源本身没有损坏。

检查新版、组件更新及桌面安装器更新共用保存的代理。版本变化、旧凭据迁移和安装更新前，会在数据目录 `backups` 内创建 SQLite 一致副本及配置／凭据备份，包含校验清单与 `RESTORE.txt` 恢复说明；备份失败会中止迁移或安装。恢复前关闭程序／容器并保留现有数据副本。备份不包含媒体、工具、任务工作区或浏览器登录配置，恢复旧任务仍依赖原目录。

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
