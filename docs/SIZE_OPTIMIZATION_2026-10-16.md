# 组件与安装包体积优化

测量环境：Windows x64、Node 24.16.0。只使用工作区隔离数据，不读取个人 Cookie 或改动现有任务。

## 结果

| 项目 | 优化前 | 优化后 |
| --- | ---: | ---: |
| FFmpeg、FFprobe 与所需库，未压缩 | 322.0 MiB | 186.2 MiB |
| 默认 JavaScript 引擎，未压缩 | Deno 92.9 MiB | QuickJS-NG 2.1 MiB |
| yt-dlp，未压缩 | 17.0 MiB | 17.0 MiB |
| 默认运行组件合计，未压缩 | 432.0 MiB | 205.2 MiB（减少 52.5%） |
| 便携 ZIP，同一主程序与压缩方式 | 193.9 MiB | 100.9 MiB（减少 47.9%） |
| 本地 NSIS 安装包 | — | 约 77.0 MiB |

统一提供包含所有运行组件的程序，不再区分全量与精简变体。安装包和便携 ZIP 使用相同程序与工具；应用内更新继续检查当前系统、架构、官方 SHA-256 和程序能否运行。

这些数字不包含系统共享的 WebView2、下载媒体、任务缓存或用户数据，是加入 MCP 之前的本地对照结果，不代表最新发行产物大小。

## 实现

- Windows FFmpeg 改用 BtbN/FFmpeg-Builds 的完整 GPL 共享版；FFmpeg、FFprobe 共用七个 DLL。没有删除编码器来换取体积。
- 构建锁使用带日期的 FFmpeg 标签与官方校验值；构建、便携打包、应用内更新均携带并校验 DLL。
- DLL 随任务冻结，执行前验证内容；缺失、被修改或额外注入的 DLL 会阻止任务运行。
- 默认捆绑 QuickJS-NG 0.17.0，通过明确的冻结路径交给 yt-dlp，不依赖随机的系统 JavaScript 环境。
- 组件缓存按文件 SHA-256 存储，任务组合对已校验缓存建立硬链接。更新 yt-dlp 不再重复复制未变化的 FFmpeg、DLL 和 JavaScript 引擎。不支持硬链接的文件系统回退为复制。
- 旧任务继续使用原来的工具和引擎；旧缓存没有自动迁移、覆盖或删除。

一次真实稳定版 → nightly 切换后的两个任务快照，逻辑文件大小合计 410.5 MiB，按 NTFS 文件标识去重后的实际载荷约 222.3 MiB；未变化组件的链接数为 3（对象缓存及两个快照），证明复用的是同一份文件。

没有改写成 Rust FFmpeg FFI：本次共享库已经消除了主要重复，同时保留现有进程取消、进度、错误诊断和离线恢复链路。

## JavaScript 速度取舍

使用相同 yt-dlp 2026.08.19、yt-dlp-ejs 0.8.0、已经缓存的相同播放器脚本，执行上游集成测试：

| 场景 | QuickJS-NG 0.17.0 | Deno 2.9.7 |
| --- | ---: | ---: |
| 七组播放器、14 个 n/signature 请求 | 26.48 秒 | 4.33 秒 |
| 预处理后的播放器缓存一致性测试 | 4.18 秒 | 0.88 秒 |

两者均通过；这些是挑战执行测试，不代表整段视频下载耗时。QuickJS-NG 的主要收益是体积，YouTube JavaScript 执行会更慢。

桌面「工具与更新」提供可选的「安装 Deno 加速解析」。Deno 程序本体约 93 MiB，安装和任务缓存还会占用空间。已安装 Deno 时，新解析与新任务优先使用 Deno；已有 QuickJS 任务保持原冻结方案。

## 验证

- Rust 工作区 54 项测试通过；前端测试、构建及 Clippy 警告检查通过。
- 9 项发布与组件脚本测试通过，包括共享 DLL 缺失、重复载荷、下载中断和校验失败。
- 4 项原生媒体验收通过，覆盖分离音视频、转码、多音轨、字幕、封面、剪辑、重启恢复、发布恢复和处理重试。
- B 站 `BV1GJ411x7h7` 实际下载：解析 1.70 秒，下载、处理和入库约 13.49 秒；最低 360p、HEVC + AAC、212.31 秒，FFmpeg 完整解码及封面检查通过。此单次记录不作为稳定性能结论。
- yt-dlp 正确识别 QuickJS-NG；上游 JavaScript 挑战与缓存测试通过。
- 应用内实际更新 FFmpeg、qjs、yt-dlp nightly 均通过；从无组件、无工具 PATH 的隔离环境按需安装也通过。
- 便携包检查内容并实际启动桌面后端与 WebView2；退出时重试清理短暂占用的临时日志。

证据在工作区未提交的 `.tmp/size-optimization-2026-10-16/`，真实下载记录在 `.tmp/site-live-2026-10-16/bilibili-shared/`。保留原组件与对照包用于复现，不属于新软件安装体积。

Docker 引擎未启动，仅检查安装脚本语法及锁中两种 Linux 架构的来源；未做 Linux、容器或各品牌 GPU 的实机验收。真实 YouTube 整段下载仍受出口 429 / 视频不可用限制，没有使用个人 Cookie；播放器挑战执行测试已通过。

上游资料：[FFmpeg 共享构建](https://github.com/BtbN/FFmpeg-Builds)、[QuickJS-NG](https://github.com/quickjs-ng/quickjs)、[yt-dlp JavaScript 运行时](https://github.com/yt-dlp/yt-dlp/wiki/EJS)、[挑战集成测试](https://github.com/yt-dlp/yt-dlp/blob/master/test/test_jsc/test_ejs_integration.py)。
