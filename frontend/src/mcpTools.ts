export type Copy = readonly [string, string];
type ToolHelp = { name: string; title: Copy; description: Copy; example: Record<string, unknown>; site?: boolean };
export const mcpGroups: { title: Copy; tools: ToolHelp[] }[] = [
  { title: ['引擎与站点', 'Engine & sites'], tools: [
    { name: 'get_ytdlp_status', title: ['引擎状态', 'Engine status'], description: ['查看 yt-dlp 版本和媒体处理依赖。', 'Check the yt-dlp version and media dependencies.'], example: {} },
    { name: 'list_extractors', title: ['站点提取器', 'Site extractors'], description: ['搜索当前引擎提供的提取器，不代表网站当前一定可用。', 'Search extractors in the installed engine; availability still requires a live probe.'], example: { query: 'bilibili', limit: 20 } },
    { name: 'list_search_providers', title: ['搜索来源', 'Search providers'], description: ['查看当前引擎支持的关键词搜索来源和前缀。', 'List keyword search providers and prefixes supported by the installed engine.'], example: {} },
  ] },
  { title: ['链接与方案', 'Links & plans'], tools: [
    { name: 'inspect_url', title: ['识别链接', 'Inspect a link'], description: ['识别媒体或合集，并显示本次解析实际提供的资料。', 'Identify media or collections and report data observed in this probe.'], example: { url: 'https://example.com/media' }, site: true },
    { name: 'get_download_options', title: ['下载参数', 'Download options'], description: ['查询可用参数、默认值和当前已保存的媒体方案。', 'Read available parameters, defaults and the saved media plan.'], example: {} },
    { name: 'preview_download', title: ['预览下载方案', 'Preview a download'], description: ['预览音画选择和处理方案，不创建下载任务。', 'Preview format selection and processing without creating a download task.'], example: { url: 'https://example.com/media', options: { quality: 'best' } }, site: true },
  ] },
  { title: ['搜索与合集', 'Search & collections'], tools: [
    { name: 'search_videos', title: ['多站点搜索', 'Search across sites'], description: ['按搜索来源查找视频或音频；省略来源时兼容使用 YouTube。', 'Search video or audio by provider; omitted providers retain the YouTube default.'], example: { provider: 'bilisearch', query: '音乐', limit: 10 }, site: true },
    { name: 'list_collection_items', title: ['合集项目', 'Collection items'], description: ['分页读取播放列表、频道或用户页，保留可下载的来源引用。', 'Page through playlists, channels or profiles and retain downloadable source references.'], example: { url: 'https://example.com/collection', limit: 20 }, site: true },
    { name: 'start_collection_download', title: ['下载选定合集范围', 'Download a collection range'], description: ['先解析选定范围再逐项入队，默认 20 项，最多 100 项。', 'Resolve a selected range before queueing each item; defaults to 20, with a maximum of 100.'], example: { url: 'https://example.com/collection', limit: 20 }, site: true },
  ] },
  { title: ['媒体资料', 'Media information'], tools: [
    { name: 'get_video_info', title: ['媒体概览', 'Media overview'], description: ['读取视频、音频、播客或剧集的来源信息和直播状态。', 'Read source metadata and live status for video, audio, podcasts or episodes.'], example: { url: 'https://example.com/media' }, site: true },
    { name: 'list_video_formats', title: ['格式与音轨', 'Formats & audio tracks'], description: ['查看画质、编码、容器、语言和估计大小。', 'Inspect resolution, codecs, containers, languages and estimated sizes.'], example: { url: 'https://example.com/media' }, site: true },
    { name: 'get_video_chapters', title: ['章节', 'Chapters'], description: ['分页读取来源提供的章节和时间范围。', 'Read a page of chapters and timestamps supplied by the source.'], example: { url: 'https://example.com/media', limit: 20 }, site: true },
    { name: 'list_video_thumbnails', title: ['来源封面', 'Source thumbnails'], description: ['查看封面尺寸和可公开地址，省略签名直链。', 'List thumbnail dimensions and public URLs, omitting signed links.'], example: { url: 'https://example.com/media' }, site: true },
    { name: 'get_channel_info', title: ['频道或作者概览', 'Channel or creator overview'], description: ['读取概览，最多发现一个项目，不遍历完整频道。', 'Read an overview with at most one discovered entry, without enumerating a channel.'], example: { url: 'https://example.com/channel' }, site: true },
  ] },
  { title: ['字幕与评论', 'Subtitles & comments'], tools: [
    { name: 'list_subtitle_languages', title: ['字幕语言', 'Subtitle languages'], description: ['查看来源提供的人工和自动字幕语言。', 'List manual and automatic subtitle languages provided by the source.'], example: { url: 'https://example.com/media' }, site: true },
    { name: 'get_video_transcript', title: ['字幕正文', 'Subtitle text'], description: ['按语言、时间范围和分页读取字幕，支持格式转换。', 'Read subtitles by language, time range and page, with format conversion.'], example: { url: 'https://example.com/media', language: 'zh-Hans', limit: 100 }, site: true },
    { name: 'get_video_comments', title: ['跨站评论', 'Comments across sites'], description: ['优先使用站点限量；其他站点完整提取后分页，受时间和大小限制。', 'Use site-native limits when available; otherwise extract all comments before paging within time and size limits.'], example: { url: 'https://example.com/media', limit: 20 }, site: true },
  ] },
  { title: ['下载与附件', 'Downloads & attachments'], tools: [
    { name: 'start_download', title: ['单条下载', 'Download one item'], description: ['创建媒体下载，支持音频、片段、直播及完整的结构化媒体选项。', 'Queue media with audio, sections, live recording and structured media options.'], example: { url: 'https://example.com/media', options: { quality: 'best', fragmentConcurrent: 4 } }, site: true },
    { name: 'start_batch_download', title: ['批量下载', 'Batch downloads'], description: ['逐项添加最多 100 个链接或来源引用，并返回各项结果。', 'Queue up to 100 links or source references and return per-item results.'], example: { urls: ['https://example.com/media-a', 'https://example.com/media-b'] }, site: true },
    { name: 'download_assets', title: ['单独保存附件', 'Save attachments only'], description: ['单独保存字幕、封面、说明或信息 JSON，不下载视频。', 'Save subtitles, thumbnails, descriptions or info JSON without downloading video.'], example: { url: 'https://example.com/media', assets: ['thumbnail', 'info'] }, site: true },
  ] },
  { title: ['后处理与媒体管理', 'Processing & media management'], tools: [
    { name: 'convert_media', title: ['转换已有媒体', 'Convert saved media'], description: ['提取音频、转换容器或编码，并处理已有附件，保留来源。', 'Extract audio, change containers or codecs and process saved attachments while preserving sources.'], example: { id: 'media-id', options: { contentMode: 'audio', audioFormat: 'mp3' } } },
    { name: 'retry_processing', title: ['重试处理', 'Retry processing'], description: ['使用保留的来源文件重试失败处理。', 'Retry failed processing using retained source files.'], example: { id: 'task-id' } },
    { name: 'list_media', title: ['搜索媒体库', 'Search the media library'], description: ['分页搜索已完成的媒体。', 'Search completed media with pagination.'], example: { query: '音乐', limit: 20 } },
    { name: 'get_media', title: ['媒体记录', 'Saved media record'], description: ['查看已保存媒体的信息，不返回内部目录。', 'Read saved media metadata without internal paths.'], example: { id: 'media-id' } },
    { name: 'list_downloads', title: ['任务列表', 'Download tasks'], description: ['分页查看任务和附件保存状态。', 'Read a page of download and attachment task statuses.'], example: { limit: 20 } },
    { name: 'get_download', title: ['任务详情', 'Task details'], description: ['查看进度、警告、错误和生成文件。', 'Inspect progress, warnings, errors and generated files.'], example: { id: 'task-id' } },
    { name: 'control_download', title: ['控制任务', 'Control a task'], description: ['暂停、继续、取消、重试、优先下载或结束直播保存。继续沿用原方案；retryCurrent 使用当前设置和登录。', 'Pause, resume, cancel, retry, prioritize or finish and save live capture. Resume keeps the original plan; retryCurrent uses current settings and sign-in.'], example: { id: 'task-id', action: 'retryCurrent' } },
    { name: 'resolve_download_filename', title: ['解决同名冲突', 'Resolve filename conflict'], description: ['选择新文件名或跳过。覆盖文件须在桌面或登录后的网页明确确认。', 'Choose a new filename or skip. Replacing files requires confirmation in the desktop or authenticated web interface.'], example: { id: 'task-id', choice: 'rename', filename: 'Video (2).mp4' } },
    { name: 'get_cookie_status', title: ['登录状态资料', 'Saved sign-in metadata'], description: ['查看 Cookie 网站、数量和同步时间，不读取明文或验证登录。', 'Read cookie sites, counts and sync times without exposing values or verifying sign-in.'], example: { url: 'https://example.com/' } },
  ] },
];

export const capabilityCoverage: { title: Copy; status: 'available' | 'site' | 'managed' | 'closed'; detail: Copy }[] = [
  { title: ['引擎与站点发现', 'Engine & site discovery'], status: 'available', detail: ['版本、提取器、搜索来源、试解析', 'Versions, extractors, search providers and URL probes'] },
  { title: ['资料、格式与筛选', 'Metadata, formats & selection'], status: 'available', detail: ['音画、音轨、合集、日期、时长和范围', 'Video, audio, tracks, collections, dates, durations and ranges'] },
  { title: ['字幕、评论与直播', 'Subtitles, comments & live media'], status: 'site', detail: ['以来源实际提供的数据和提取器能力为准', 'Depends on data and extractor features provided by the source'] },
  { title: ['附件与后处理', 'Attachments & post-processing'], status: 'available', detail: ['外挂、嵌入、封装、转码、抽音频及章节拆分', 'Sidecars, embedding, remuxing, recoding, audio extraction and chapter splitting'] },
  { title: ['SponsorBlock 与从头录制', 'SponsorBlock & live-from-start'], status: 'site', detail: ['SponsorBlock 限 YouTube；从头录制限引擎支持的站点', 'SponsorBlock is YouTube-specific; live-from-start requires extractor support'] },
  { title: ['认证、网络与保存位置', 'Authentication, network & storage'], status: 'managed', detail: ['自动 Cookie、代理、目录和命名沿用应用设置', 'Automatic cookies, proxy, directories and naming use app settings'] },
  { title: ['更新、缓存与运行时', 'Updates, caches & runtimes'], status: 'managed', detail: ['通过应用管理，MCP 查询不暴露内部路径', 'Managed by the app; MCP queries do not expose internal paths'] },
  { title: ['任意命令、插件与本地路径', 'Arbitrary commands, plugins & paths'], status: 'closed', detail: ['不接受任意 CLI、执行程序或本地文件入口', 'Arbitrary CLI arguments, executables and local file inputs are not accepted'] },
];
