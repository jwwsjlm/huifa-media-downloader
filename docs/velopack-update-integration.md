# Velopack 本程序更新集成基线

核验日期：2026-08-27
目标版本：Velopack Python SDK / `vpk` 1.2.0

当前发行统一使用 Velopack：GitHub Release 提供便携版 ZIP 和安装包 ZIP，二者共享
同一套更新 feed、完整包、检查界面和安装确认流程。便携包使用 Velopack 标准目录结构，
应用位于 `current/`，用户数据位于根目录 `data/`，更新时只替换受管应用目录。

## 官方能力与限制

- Python 包与 `vpk` 工具固定为 1.2.0，当前发行目标为 Windows x64。
- `velopack.App().run()` 在程序入口尽早执行，以处理安装、卸载和更新钩子。
- `GithubSource` 从写死的公开 GitHub 仓库读取 Release；公共仓库不要求用户配置 Token。
- `UpdateManager` 负责检查、下载进度、待重启更新和退出后安装。
- `vpk pack --releaseNotes` 把 Markdown 版本说明写入更新包；应用同时按目标 tag 读取
  GitHub Release 正文，API 不可用时回退包内说明。
- `vpk pack` 生成安装器、可自更新便携包、完整包和 JSON feed；存在上一版本时可生成增量包。
- Velopack 使用 PyInstaller onedir，更新单位是完整应用目录。
- Windows 更新会替换 `current/`，因此数据库、设置、Cookie、日志和可独立更新工具必须位于
  根目录 `data/` 等持久目录。
- 正式公开发行仍建议增加 Windows 代码签名，降低 SmartScreen 和安全软件拦截概率。

官方资料：

- <https://docs.velopack.io/getting-started/python>
- <https://docs.velopack.io/reference/py>
- <https://docs.velopack.io/reference/py/Sources/GithubSource>
- <https://docs.velopack.io/reference/py/UpdateManager>
- <https://docs.velopack.io/integrating/preserved-files>
- <https://docs.velopack.io/integrating/release-notes>
- <https://docs.velopack.io/packaging/overview>

## 本项目接线

- `app/core/application_updater.py`
  - 固定并规范化官方 GitHub 仓库；
  - 封装 `GithubSource`、`UpdateManager` 和 `UpdateOptions`；
  - 提供版本、大小、SHA-256、版本说明和下载进度；
  - 安装前要求用户明确确认；
  - 提供 24 小时自动检查节流和 Velopack 持久目录识别。
- `build/HuifaVideoDownloader.velopack.spec`
  - 生成 PyInstaller onedir 应用；
  - 保留 Qt 和运行依赖精简规则。
- `build/01_velopack_hook.py`
  - 在主程序启动前运行 Velopack 钩子；
  - 不在未确认时自动安装更新。
- `scripts/build_velopack_release.ps1`
  - 生成安装器、便携包、完整包和 feed；
  - 校验版本、工具链、包结构、大小和 SHA-256；
  - 在隔离目录真实启动便携包，并执行一次本地更新 apply；
  - 验证 `current/tools/` 随更新恢复且根目录 `data/` 保留。
- `scripts/package_github_release.ps1`
  - 输出面向用户的便携版 ZIP、安装包 ZIP、版本说明、feed、完整包和校验清单；
  - 不再发布独立单 EXE 资产。

构建前可执行快速环境探测；它不会运行 PyInstaller 或生成发行文件：

```powershell
.\scripts\build_velopack_release.ps1 -Version 0.1.0 -ValidateEnvironmentOnly
```

稳定版使用 `win` 通道，预览版必须使用独立通道，例如：

```powershell
.\scripts\build_velopack_release.ps1 -Version 0.2.0
.\scripts\build_velopack_release.ps1 -Version 0.2.0-beta.1 -Channel win-beta
```

发布脚本本身不会上传 GitHub；上传只由标签触发的 GitHub Actions 工作流执行。
