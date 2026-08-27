# Velopack 本程序更新集成基线

核验日期：2026-08-24  
目标版本：Velopack Python SDK / `vpk` 1.2.0

> 当前发行决策：GitHub Release 同时提供便携版 ZIP 和安装包 ZIP。便携版 ZIP 包含
> `scripts/build_release.ps1` 生成的单文件 `HuifaVideoDownloader.exe`；安装包 ZIP 包含
> `scripts/build_velopack_release.ps1` 生成的 Velopack `Setup.exe`。Release 还会保留原始
> 单 EXE 作为便携版自动更新载荷，并上传 Velopack feed 与完整包供安装版自动更新。
> `scripts/package_github_release.ps1` 负责统一封装两个 ZIP、复制版本说明并生成校验清单。

## 已核验的官方能力与限制

- Python 包 1.2.0 于 2026-06-03 发布，支持 Windows x64、x86 和 ARM64；本项目锁定 Windows x64。
- `velopack.App().run()` 必须尽可能早执行，因为安装、卸载和更新钩子可能要求当前进程提前退出或重启。
- Python `UpdateManager` 提供检查、带进度下载、读取待重启更新、安装并重启、退出后安装等接口。
- `GithubSource(repo_url, access_token=None, prerelease=False)` 直接读取 GitHub Releases。公共仓库不需要 Token，但会受 GitHub 每 IP 每小时 60 次匿名请求限制；私有仓库必须提供 Token。
- 更新包可携带 Markdown/HTML 更新说明；`vpk pack --releaseNotes` 将说明写入包，检查更新时即可读取。
- `vpk pack` 默认生成：完整更新包、`Setup.exe` 安装器、可自更新 `Portable.zip`、更新 feed；存在上一版本时还可生成增量包。可选 `--msi` 生成 MSI。
- Velopack 明确要求 PyInstaller `--onedir`，不兼容 `--onefile`。原因是更新单位是整个应用目录，而不是单个自解压 EXE。
- Windows 更新会完整替换 `current` 目录。因此数据库、设置和日志不能继续存放在 EXE 同目录；应放在 `current` 上一级或 `%AppData%`。
- 官方建议生产发行进行代码签名，否则安装器/可执行文件可能被 Windows 或安全软件拦截。

官方资料：

- <https://docs.velopack.io/getting-started/python>
- <https://docs.velopack.io/reference/py>
- <https://docs.velopack.io/reference/py/Sources/GithubSource>
- <https://docs.velopack.io/reference/py/UpdateManager>
- <https://docs.velopack.io/integrating/preserved-files>
- <https://docs.velopack.io/integrating/release-notes>
- <https://docs.velopack.io/packaging/overview>
- <https://docs.velopack.io/reference/cli/content/vpk-windows>
- <https://pypi.org/project/velopack/>

## 本项目新增的独立基础

- `app/core/application_updater.py`
  - 严格规范化 GitHub `owner/repository`；
  - 封装 `GithubSource`、`UpdateManager` 和 `UpdateOptions`；
  - 将原生对象转换成不依赖 Qt 的更新信息；
  - 提供更新说明、大小、SHA-256、便携/安装模式信息；
  - 下载进度归一化到 0–100；
  - 安装前强制显式确认；
  - Token 不写入配置，错误消息会脱敏；
  - 提供 24 小时自动检查节流记录；
  - 提供 Velopack `current` 外部数据目录识别。
- `build/HuifaVideoDownloader.velopack.spec`
  - 独立 PyInstaller onedir 构建；
  - 保留现有 Qt/FFmpeg 精简规则；
  - 不替换当前 onefile spec。
- `build/01_velopack_hook.py`
  - 在主程序之前运行 Velopack 启动逻辑；
  - 禁用未确认的启动时自动安装。
- `scripts/build_velopack_release.ps1`
  - 只在本地构建，不上传 GitHub；
  - 版本必须与 `APP_VERSION` 一致；
  - Python SDK 与 `vpk` 均锁定 1.2.0；
  - 不信任 `PATH` 中第一个 `dotnet.exe`：优先检查 64 位 Program Files，逐个候选执行
    `--list-sdks`，确认存在 SDK 后，恢复工具、下载历史版本和打包全部使用同一个绝对路径；
  - 预发布 SemVer 禁止写入稳定 `win` 通道，建议使用 `win-beta`；
  - 打包后解析并核验两个 JSON feed，校验当前版本完整包的大小、SHA-256、ZIP 结构，
    同时验证安装器、便携包和可选 MSI 都真实存在且非空；
  - 支持 Setup 安装器、可自更新便携版和可选 MSI；
  - 可从既有 GitHub Release 下载上一版本，以生成增量包。

构建前可运行快速环境探测；它不会清理目录、运行 PyInstaller 或生成发行文件：

```powershell
.\scripts\build_velopack_release.ps1 -Version 0.1.0 -ValidateEnvironmentOnly
```

正式稳定版使用默认 `win` 通道；预览版必须使用独立通道，并让对应发行构建沿用该通道：

```powershell
.\scripts\build_velopack_release.ps1 -Version 0.2.0
.\scripts\build_velopack_release.ps1 -Version 0.2.0-beta.1 -Channel win-beta
```

不要为了预览版修改稳定通道 feed。Velopack 受管安装默认会记住它所属的构建通道；应用侧
如果显式覆盖通道，必须与安装包的通道一致，否则会检查到错误的 feed。

## 若未来切换到 Velopack 的接线方案

1. 在 `app/core/paths.py` 的 `data_dir()` 中优先调用 `velopack_persistent_data_dir()`；检测到受管发行时使用 `{RootAppDir}/data`，再迁移旧 `current/data`。迁移必须先备份并使用一次性标记。
2. 设置页增加：应用更新仓库、启动后自动检查、是否接收预发布版。仓库默认值应在正式仓库确定后写入，Token 只从环境变量或安全存储读取。
3. 启动 5–10 秒后在后台线程调用 `AutoUpdateCheckThrottle.is_due()`，自动检查只处理本程序，不检查 yt-dlp/FFmpeg 等组件。
4. 检查到更新后展示：当前/目标版本、包大小、更新说明、安装版或便携版标记，并让用户选择“稍后”或“下载更新”。
5. 下载完成后再次明确询问“立即重启安装”或“退出时安装”。主窗口应先停止下载、发布、浏览器和更新线程，再退出进程。
6. 首次上线前至少用两个本地版本完成：安装版升级、便携版升级、增量失败回退完整包、设置/数据库保留、占用文件恢复、取消下载、断网重试和回滚验证。

## 当前未执行

- 没有调用 `vpk upload`，也没有向 GitHub 创建 Release 或上传文件。
- 当前开发机的 `PATH` 首项是仅含 Runtime 的 x86 dotnet，但 64 位 Program Files 中已安装
  .NET SDK 9.0.313 和 10.0.400。环境探测已验证脚本会选择
  `C:\Program Files\dotnet\dotnet.exe`，不会再误报“没有 SDK”。本轮没有执行耗时的真实
  PyInstaller + `vpk pack` 全构建。
- 尚未修改 `main_window.py` 或当前数据目录，以避免与正在进行的主线 UI/稳定性改动冲突。
