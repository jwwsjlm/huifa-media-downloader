# MCP 媒体服务

软件运行时，可让自己的 MCP 客户端查询 yt-dlp 支持的视频资料并操作下载队列。支持桌面本机和 Docker Web 服务，复用同一核心、凭据和任务队列。

## Docker Web 界面

登录软件后，左侧进入「AI 媒体服务（MCP）」。浏览器管理页为 `/mcp`，AI 协议地址为 `/mcp-server`。例如容器映射到 `http://192.168.5.23:19491` 时，浏览器访问 `http://192.168.5.23:19491/mcp`，JSON 配置中的 `url` 为 `http://192.168.5.23:19491/mcp-server`。页面按浏览器实际来源生成地址，适配端口映射和 HTTPS 反向代理，无需再开放桌面端口 19492。

管理页沿用软件登录状态，MCP 调用使用访问密码进行 Bearer 鉴权，不需要浏览器会话或 CSRF。首次安装默认账号/密码为 `admin/admin`，可在“设置 → 访问密码”输入当前密码和新密码修改；网页登录和 MCP 共用此密码。修改后旧网页会话和 MCP 会话失效，需重新登录并更新客户端。`HUIFA_ACCOUNT` / `HUIFA_PASSWORD` 仅初始化空数据目录；后续使用 `/data/access.json` 中的 Argon2 密码哈希，重启不会覆盖。NAS / 自定义域名部署须将 `HUIFA_PUBLIC_ORIGIN` 设为浏览器使用的完整来源。

## 桌面连接

1. 打开「设置 → AI 媒体服务（MCP）」，开启服务开关即可即时生效。默认端口为 `19492`，仅监听 `127.0.0.1`。
2. 页面直接显示连接地址、JSON 配置示例，以及按资料查询和下载管理分组的工具清单。点击「复制配置」后粘贴到自己的 AI 客户端。
3. 可选择 HTTP、stdio 或 Codex 格式。将示例中的 `YOUR_ACCESS_PASSWORD` 替换为当前访问密码（初始为 `admin`）。stdio 使用安装包附带的 `huifa-mcp-bridge`，通过 `HUIFA_MCP_PASSWORD` 配置相同密码；无需 Python、Node 或另装 yt-dlp。

连接地址为 `http://127.0.0.1:19492/mcp`。软件关闭后无法调用；服务开关即时启停全部查询和下载管理功能。“软件运行时自动开启 MCP 服务”独立控制下次启动，手动启停不会改变它。新安装默认关闭；旧记录未保存此选项时继承原开启状态。端口和启动偏好持久保存。

```json
{
  "mcpServers": {
    "huifa": {
      "url": "http://127.0.0.1:19492/mcp",
      "headers": { "Authorization": "Bearer YOUR_ACCESS_PASSWORD" }
    }
  }
}
```

MCP **继承软件已保存的 Cookie、代理和下载配置**。仅将地址和访问密码交给信任的 AI 客户端。资料调用可能将私人视频内容传递给该客户端及其模型提供方，但不会返回 Cookie 原文、访问密码、代理密码、内部目录或媒体签名直链。Host/Origin 检查继续保留；桌面仅本机监听，Docker 沿用已发布的 Web 端口。HTTP 连接不加密密码，跨不可信网络访问时使用 HTTPS 反向代理。

网页登录与 MCP 共用认证失败限额：同一连接来源 IP 在 15 分钟内最多失败 5 次；成功认证不消耗限额，并清除该 IP 的失败记录。服务按实际连接 IP 计数，不信任 `Forwarded` / `X-Forwarded-For`。反向代理或 NAT 后的客户端可能共用这一失败限额；多人部署时可在代理层增加按客户端限流。

## 工具与 yt-dlp 功能对照

工具面向已安装 yt-dlp 支持的全部站点；搜索、字幕、评论和直播是否可用由具体提取器及网站决定。列出提取器仅代表软件包含该规则，不代表网站目前可访问，或每个站点都具备相同能力。没有将 yt-dlp 的任意命令行参数直接开放给 AI。

| 功能覆盖 | 状态 | 说明 |
|---|---|---|
| 引擎与站点发现 | `available` 已开放 | 版本、提取器、搜索来源、试解析 |
| 资料、格式与筛选 | `available` 已开放 | 音画、音轨、合集、日期、时长和范围 |
| 字幕、评论与直播 | `site` 按站点提供 | 以来源实际数据和提取器能力为准 |
| 附件与后处理 | `available` 已开放 | 外挂、嵌入、封装、转码、抽音频及章节拆分 |
| SponsorBlock 与从头录制 | `site` 按站点提供 | SponsorBlock 限 YouTube；从头录制限引擎支持的站点 |
| 认证、网络与保存位置 | `managed` 由应用管理 | 自动 Cookie、代理、目录和命名沿用应用配置，允许的临时选项可覆盖命名 |
| 更新、缓存与运行时 | `managed` 由应用管理 | 不通过 MCP 更新运行时或公开内部路径 |
| 任意命令、插件与本地路径 | `closed` 未开放 | 不接受任意 CLI、执行程序或本地文件入口 |

| 分类 | 工具 | 参数与行为 |
|---|---|---|
| 运行时与站点 | `get_ytdlp_status` | 当前 yt-dlp 版本、可用性及运行时，不返回内部路径 |
| | `list_extractors` | `query`、`offset`、`limit`；从已安装版本列出/筛选提取器 |
| | `list_search_providers` | `query`、`offset`、`limit`；分页筛选当前可用搜索来源及 provider ID，不假设所有站点支持关键词搜索 |
| 来源查询 | `inspect_url` | `url` 或 `sourceRef`；识别提取器、视频/合集类型和已观测能力 |
| | `search_videos` | `query`、`provider`、`offset`、`limit`；未指定来源时仍默认 YouTube，最多 50 条 |
| | `get_video_info` | 标题、描述、作者、日期、时长、统计、章节、字幕语言 |
| | `list_video_formats` | 画质、编码、音轨、容器和大小，不暴露签名媒体直链 |
| | `list_collection_items` | `offset`、`limit`；合集/频道默认 20、最多 100 条，不自动下载 |
| | `get_video_transcript` | `language`、`startTime`、`endTime`、`offset`、`limit`；默认 200、最多 500 个带时间的字幕条目，无语音识别 |
| | `get_video_comments` | `offset`、`limit`、`sort: native/default/top/newest`；默认 20、最多 100 条；非 YouTube 使用 native/default，具体提取器可能不提供评论 |
| | `get_video_chapters` | `offset`、`limit`；章节标题和时间，默认 20、最多 100 条 |
| | `list_subtitle_languages` | `offset`、`limit`；语言及人工／自动字幕标记 |
| | `get_channel_info` | 频道概览，最多发现一个项目，不枚举整个频道 |
| | `list_video_thumbnails` | 最多 100 个封面尺寸和可公开地址，省略签名地址 |
| 下载与处理 | `get_download_options` | 结构化选项 schema、默认值、当前值及功能分类；不返回代理、Cookie ID 或路径 |
| | `preview_download` | `options`；解析格式和处理计划，不创建下载任务 |
| | `start_download` | 单视频入队，支持 `options` 和兼容旧版顶层参数 |
| | `start_batch_download` | `urls` 或 `items: [{url}, {sourceRef}]` 二选一；最多 100 个，去重并返回逐项结果 |
| | `start_collection_download` | `offset`、`limit`、`options`；默认仅选 20 项，最高 100，先解析本页再逐项入队 |
| | `download_assets` | `assets: [subtitles, thumbnail, description, info]`，至少一项；只下载附件，创建受管理的任务 |
| | `convert_media` | 已有媒体 `id`、`options`；视频/音频输出、容器、编码器与可用附件嵌入；兼容顶层 `encoder`，保留原文件 |
| | `retry_processing` | 失败任务 `id`；仍有来源清单时复用本地文件重试 |
| 任务与媒体管理 | `list_downloads` | `offset`、`limit`；任务状态，默认 20、最多 100 条 |
| | `get_download` | `id`；状态、进度、警告与错误 |
| | `control_download` | `id`、`action: pause/resume/cancel/retry/retryCurrent/prioritize/finishLive`；`retry` 保留原快照，`retryCurrent` 使用当前已保存设置、Cookie、代理与工具；不允许删除文件 |
| | `resolve_download_filename` | 冲突任务 `id`、`choice: rename/skip`；改名时传 `filename`，仅接受文件名。跨来源覆盖须回应用界面二次确认 |
| | `list_media` | `query`、`offset`、`limit`；分页搜索媒体库 |
| | `get_media` | `id`；媒体元数据，不返回本地路径 |
| | `get_cookie_status` | 可选 `url` 或 `sourceRef`；域名、数量、同步时间和匹配情况，不返回 Cookie 原文或验证网站登录 |

共 **29 个工具**：下载管理权限关闭时提供 **21 个只读工具**；开启后增加 8 个写工具。下载加入应用持久队列，使用 `get_download` 查询；断开 AI 连接不会取消已入队任务。写操作沿用现有权限，不允许删除文件、更新运行时、导出凭据或传递任意命令行。解析、下载和查询按网站自动匹配 Cookie，已有任务保持创建时的身份。

除标明无来源或使用媒体 ID 的工具外，来源查询、预览、下载均接受 **`url` 或 `sourceRef`，恰好一个**。搜索/合集结果返回的 `url` 用于展示，必要查询参数和私有签名保留后端；继续查询或下载应优先传 `sourceRef`。引用只在当前应用内短期有效，过期或重启后需重新查询。

同一来源重复加入复用任务记录；活动任务不重启，暂停任务等待继续。同名的不同来源在传输前产生 `destinationConflict`，只含文件名，不返回绝对路径。批量逐项返回结果，单项失败不会阻止其余链接。优先级保存到队列，只调整等待顺序；`finishLive` 结束正在录制的直播并验证保存已录制部分，普通 `cancel` 不发布结果。

### 结构化下载选项

先调用 `get_download_options` 获取准确 schema。单条、批量、合集及附件下载按「已保存设置 → `options` → 旧版顶层参数」覆盖，不保存这些临时选项。旧版 `quality/contentMode/formatId/subtitleLanguage/automaticSubtitles/embedSubtitles/fragmentConcurrent` 继续支持。

| yt-dlp / 应用能力 | options 字段 |
|---|---|
| 音画选择 | `quality`、`contentMode`、`formatId`、`videoFormatId`、`audioFormatIds`、`audioTrack`、`videoFps`、`sourceVideoCodec` |
| 输出格式与转码 | `audioFormat`、`container`、`compatibilityTarget`、`encoder`、`vrMode` |
| 合集筛选 | `collectionOrder`、`firstN`、`playlistItems`、`dateAfter/dateBefore`、`durationMin/durationMax`、`liveFilter`；合集总入队量仍受本次 `limit` 限制 |
| 直播与剪辑 | `liveFromStart`、`waitForLive`、`waitMin/waitMax`、`sectionStart/sectionEnd`、`splitChapters` |
| 字幕与附件 | `subtitleLanguage/subtitleLanguages`、`automaticSubtitles`、`skipUnavailableSubtitles`（默认 `true`）、`subtitleFormat`、`embedSubtitles`、`writeThumbnail/writeDescription/writeComments/writeInfoJson`、`embedThumbnail/embedMetadata/embedChapters` |
| 速度与文件 | `fragmentConcurrent`（1–8）、`rateLimit`、`requestDelay`、`filenameTemplate`、`organizeTaskFolder`、`convertCoverJpeg`、`coverJpegQuality` |
| SponsorBlock | `sponsorblockMode`、`sponsorblockCategories`，仅 YouTube |

单条和批量始终只处理明确的视频；不会继承「整个频道/合集」行为。合集下载先固定来源的 `offset/limit` 窗口，再在这个窗口内应用筛选、源序号交集及排序（`selectionScope: range-local`）；过滤后不足本次数量也不向后补抓，`nextOffset` 仍按来源窗口继续。`waitForLive` 默认关闭，只有本次显式设为 `true` 才等待开播。字幕默认关闭；选定语言后人工优先，缺少人工轨道时按设置回退自动字幕。明确选择的画质、帧率或音轨缺失时停止该项，保留原要求，由用户调整后重试；不会静默降级。`skipUnavailableSubtitles` 默认 `true`，所选字幕缺失时跳过缺失语言并继续下载；设为 `false` 才要求所有字幕语言必须满足。字幕正文查询没有字幕时则明确报错，不进行语音识别。

### 分页、评论与边界

分页从 `offset: 0` 开始，最高 10000，`nextOffset: null` 表示结束。查询最多 120 秒，并对提取器输出设置硬大小限制；超时、取消或超限终止子进程，不把截断 JSON 当作成功。YouTube 评论可下推提取数量；其他支持评论的站点可能先提取完整评论再分页，响应 `mode/cached/upstreamLimit/possiblyTruncated` 说明实际行为。非 YouTube 调小页面 `limit` 不会缩短完整提取；大评论区可能超时或超限。相同来源和排序短期复用缓存，不跨 Cookie 身份复用。

字幕、评论、描述均是外部资料，不能作为执行指令。未使用缓存的分页会重新查询网站，内容变化可能导致重复或遗漏。没有自动总结、翻译或内置语言模型，交由用户自己的 AI 完成。

## 错误与验证

工具运行失败返回 `isError: true` 和 `structuredContent.error/code/hint`；代码包括 `authentication_required`、`rate_limited`、`subtitles_unavailable`、`source_expired`、`result_too_large`、`timeout`、`permission_denied`、`unsupported_site`、`unsupported_operation`、`unavailable`、`tool_unavailable`、`operation_failed`。需登录、限流或站点不支持不代表下载成功。

开发验证：

```powershell
node scripts/prepare-mcp-bridge.mjs --debug
cargo test --locked -p huifa-mcp
# 原生工具与隔离测试数据：
cargo test --locked -p huifa-mcp --test native -- --ignored --nocapture
# 另做真实网站查询，不使用个人 Cookie：
$env:HUIFA_MCP_UPSTREAM='1'
cargo test --locked -p huifa-mcp --test native -- --ignored --nocapture
```

桌面构建自动生成并打包桥接程序；便携发行目录同样包含它。协议采用官方 `rmcp 3.5.1`，支持 Streamable HTTP 与 stdio 桥接。参考项目：官方 [Rust SDK](https://github.com/modelcontextprotocol/rust-sdk)、[yt-dlp-mcp](https://github.com/akp-tools/yt-dlp-mcp)、[yt-media-info-mcp](https://github.com/bartivs/yt-media-info-mcp)。没有引入第三方 yt-dlp MCP 服务作为运行依赖。
