# Rust 开发、验收与发布

## 目录与依赖

- `crates/core`：下载队列、固定处理计划、SQLite、加密凭据、进程监督、FFmpeg 与封面。
- `crates/mcp`：复用核心的 Streamable HTTP MCP 协议服务和 stdio 桥接程序；桌面与 Docker 只保留一个服务开关。
- `src-tauri`：桌面窗口、受控文件访问、通知、更新和退出保存。
- `crates/server`：认证网页、SSE、受控下载与一次性远程浏览器。
- `frontend`：两个宿主共用的 React 界面；`languages` 保存中英文语言包。
- `scripts` 和 `.github/workflows`：工具版本、版本同步、发行产物与发布。

使用 Node.js 24 和 Rust stable。Windows 需要 Visual Studio C++ Build Tools 和 WebView2；Ubuntu 桌面构建需要 `libwebkit2gtk-4.1-dev libappindicator3-dev librsvg2-dev patchelf libssl-dev build-essential`。Docker 依赖由 Dockerfile 提供。

## 本机运行

在仓库根目录执行：

```sh
npm ci --prefix frontend
node scripts/acquire-rust-tools.mjs
npm --prefix frontend run tauri -- dev
```

工具按 `runtime-tool-lock.json` 固定版本和 SHA-256 校验。本地 `runtime-tools` 与编译产物不提交。开发时可用 `HUIFA_DATA_DIR`、`HUIFA_DOWNLOAD_DIR` 指定隔离数据目录。

加入任务时把完整工具集合保存到数据目录的 `tools/frozen/<内容哈希>/`，任务始终使用该副本并验证校验值。应用或容器升级不会让已有任务改用新工具。备份和恢复处理中任务时，应同时保留这个目录与原始媒体输入。

网页本机运行先构建前端，再给 `huifa-server` 设置 `HUIFA_DATA_DIR`、`HUIFA_DOWNLOAD_DIR`、`HUIFA_FRONTEND_DIR`、`HUIFA_TOOL_DIR`、`HUIFA_ACCOUNT`、`HUIFA_PASSWORD` 和 `HUIFA_PUBLIC_ORIGIN`。生产部署见 [Docker 文档](../docker/README.md)。

## 验收

```sh
npm run check --prefix frontend
npm test --prefix frontend
npm run build --prefix frontend
node --test tests/*.test.mjs
node scripts/acquire-rust-tools.mjs --self-test
cargo fmt --all --check
cargo test --locked --release -p huifa-core -p huifa-mcp -p huifa-server -- --include-ignored --test-threads=1
cargo clippy --locked -p huifa-core -p huifa-mcp -p huifa-server --all-targets -- -D warnings
cargo test --locked -p huifa-mcp -p huifa-server
node scripts/prepare-mcp-bridge.mjs --debug
cargo test --locked -p huifa-desktop
```

真实媒体测试需要已下载的官方工具；`HUIFA_TEST_TOOLS` 可指定其目录。使用发布构建运行验收，使大型冻结工具的校验耗时与发行程序一致。测试覆盖分离音视频、队列预算、处理失败离线恢复、发布日志恢复、多音轨、字幕重定时、章节切片、封面片头及音频输出。

性能基准、优化前后结果和复现方式见 [2026-10-16 性能审计](PERFORMANCE_AUDIT.md)。

容器执行 `node scripts/check-rust-container.mjs` 和 `node docker/check-browser.mjs`，用 `HUIFA_IMAGE`、`HUIFA_PLATFORM` 指定镜像。它们创建并清理独立测试容器和数据卷。

## GitHub Actions 正式发布

从默认分支运行 **Release**，填写 `version`（例如 `0.3.0`）和 Markdown `release_notes`。仓库需要 `TAURI_SIGNING_PRIVATE_KEY`、`TAURI_SIGNING_PRIVATE_KEY_PASSWORD` 两个 Secrets；Tauri 配置的公钥必须对应。私钥不可提交到 Git。

```sh
gh workflow run release.yml --ref main -f version=0.3.0 -F release_notes=@release-notes/0.3.0.md
```

工作流验证版本与说明，生成包含版本、锁文件和工具版本的一个提交；共享前端只构建一次。Windows、Linux 桌面和原生 amd64、ARM64 容器基于同一提交构建并验收。

全部成功后才生成版本标签和 GitHub Release，发布签名 Windows 安装包、Windows 便携 ZIP、Linux AppImage、`rust-updates.json`、`SHA256SUMS`，并发布 Docker 多架构版本及 `latest`。已发布版本不可覆盖。中断时使用 **Re-run failed jobs** 恢复同一个构建；另发版本时重新填写版本号和说明。

Windows 构建通过 `scripts/package-windows-portable.ps1` 打包原生程序、MCP 桥接程序及校验过的全部工具，执行隔离启动检查，再用同一 Tauri 密钥签名 ZIP。`portable.flag` 禁用安装包更新入口；便携版沿用系统用户数据目录，更新方式为手动解压覆盖程序。发布门禁要求安装包和便携 ZIP 均有签名，两者都进入 `SHA256SUMS`。

为已有发行版补便携包时，在 **Release** 勾选 `portable_only`，填写当前源码一致的版本号和原有说明。此模式只增加 ZIP、签名和独立的 `.zip.sha256` 校验清单，保留原有产物、更新清单和标签；ZIP 内的说明记录实际构建提交。

旧 Python 实现可从 Git 基线 `bf79d1a` 找回。当前源码、检查和发布以 Rust 为准。
