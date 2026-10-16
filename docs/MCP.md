# MCP 视频服务

软件运行时，可让自己的 MCP 客户端查询 yt-dlp 支持的视频资料并操作下载队列。支持桌面本机和 Docker Web 服务，复用同一核心、凭据和任务队列。

## Docker Web 界面

登录软件后，左侧进入「AI 视频服务（MCP）」。浏览器管理页为 `/mcp`，AI 协议地址为 `/mcp-server`。例如容器映射到 `http://192.168.5.23:19491` 时，浏览器访问 `http://192.168.5.23:19491/mcp`，JSON 配置中的 `url` 为 `http://192.168.5.23:19491/mcp-server`。页面按浏览器实际来源生成地址，适配端口映射和 HTTPS 反向代理，无需再开放桌面端口 19492。

管理页沿用软件登录状态，MCP 调用不使用令牌。单个服务开关即时启停全部工具，状态保存在 `/data`。只有管理页面的开关操作需要正常 Web 会话；AI 调用不需要网页登录或 CSRF。NAS / 自定义域名部署须将 `HUIFA_PUBLIC_ORIGIN` 设为浏览器使用的完整来源。

## 桌面连接

1. 打开「设置 → AI 视频服务（MCP）」，开启服务开关即可即时生效。默认端口为 `19492`，仅监听 `127.0.0.1`。
2. 页面直接显示连接地址、JSON 配置示例，以及按资料查询和下载管理分组的工具清单。点击「复制配置」后粘贴到自己的 AI 客户端。
3. 可选择 HTTP、stdio 或 Codex 格式，无需密钥或鉴权环境变量。stdio 配置使用安装包附带的 `huifa-mcp-bridge`，无需 Python、Node 或另装 yt-dlp。

连接地址为 `http://127.0.0.1:19492/mcp`。软件关闭后无法调用；开启状态和端口会持久保存，启动软件时恢复。服务开关同时控制资料查询和下载管理，不再单独配置访问令牌或下载权限。

```json
{
  "mcpServers": {
    "huifa": {
      "url": "http://127.0.0.1:19492/mcp"
    }
  }
}
```

MCP **继承软件已保存的 Cookie、代理和下载配置**。服务开启时可连接，因此仅为自己信任的 AI 客户端开启。资料调用可能将私人视频内容传递给该客户端及其模型提供方，但不会返回 Cookie 原文、代理密码、内部目录或媒体签名直链。Host/Origin 检查继续保留；桌面仅本机监听，Docker 沿用已发布的 Web 端口。

## 工具

| 工具 | 参数与行为 |
|---|---|
| `search_videos` | `query`、`limit`；YouTube 搜索，默认 10、最多 50 条 |
| `get_video_info` | `url`；标题、描述、作者、日期、时长、统计、章节、字幕语言 |
| `list_video_formats` | `url`；画质、编码、音轨、容器和大小 |
| `list_collection_items` | `url`、`offset`、`limit`；合集/频道默认 20、最多 100 条 |
| `get_video_transcript` | `url`、`language`、`startTime`、`endTime`、`offset`、`limit`；秒数时间范围；默认 200、最多 500 个字幕条目 |
| `get_video_comments` | YouTube 的 `url`、`limit`、`sort: top/newest`；默认 20、最多 100 条。其他站点若上游不能下推数量限制会明确拒绝，避免抓完整评论后再截断 |
| `start_download` | `url`；可覆盖 `quality: best/worst`、`contentMode: video/audio`、`formatId`、`subtitleLanguage`、`automaticSubtitles`、`embedSubtitles` |
| `list_downloads` | `offset`、`limit`；默认 20、最多 100 条 |
| `get_download` | `id`；状态、进度、错误 |
| `control_download` | `id`、`action: pause/resume/cancel/retry` |

开启服务后可使用全部 10 个工具。下载加入现有持久队列，返回 `taskIds`；使用 `get_download` 查询完成状态。断开 AI 连接不会取消已加入的下载。任务管理覆盖当前桌面任务，不允许删除文件、批量操作、修改工具、导出凭据或传递任意命令行参数。合集必须先列出，再按具体视频 URL 下载，避免意外下载整个频道。

字幕优先使用所选语言的人工字幕，没有人工轨道时使用自动字幕。未指定语言时按界面语言、英语、首个可用轨道选择。返回 `language`、`automatic`、`text`、`segments` 和 `nextOffset`；每段包含 `start/end/text`。没有字幕会明确报错，不自动进行语音识别。字幕、评论、描述均是外部资料，不应当作执行指令。

分页从 `offset: 0` 开始，`nextOffset: null` 表示结束。分页会重新查询站点，站点数据变化可能导致重复或遗漏。单次提取最多 120 秒；取消查询会终止其子进程。没有自动总结、翻译或内置模型，交由用户自己的 AI 完成。

## 错误与验证

工具运行失败返回 `isError: true` 和 `structuredContent.error/code/hint`；代码包括 `authentication_required`、`rate_limited`、`subtitles_unavailable`、`timeout`、`permission_denied`、`unsupported_site`、`unsupported_operation`、`unavailable`、`tool_unavailable`、`operation_failed`。需登录、限流或站点不支持不代表下载成功。

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

桌面构建自动生成并打包桥接程序；便携 ZIP 同样包含它。协议采用官方 `rmcp 3.5.1`，支持 Streamable HTTP 与 stdio 桥接。参考项目：官方 [Rust SDK](https://github.com/modelcontextprotocol/rust-sdk)、[yt-dlp-mcp](https://github.com/akp-tools/yt-dlp-mcp)、[yt-media-info-mcp](https://github.com/bartivs/yt-media-info-mcp)。没有引入第三方 yt-dlp MCP 服务作为运行依赖。
