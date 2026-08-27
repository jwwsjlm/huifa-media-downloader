# 构建目录约定

项目根目录只保留源码、运行组件和正式发行目录。构建过程产生的中间文件统一放在 `build/`：

- `build/HuifaVideoDownloader.velopack.spec`：可复现的 PyInstaller onedir 配置；
- `build/01_velopack_hook.py`：Velopack 构建钩子；
- 其他目录和报告均为可重新生成的临时构建产物，不保留在工作区中。

发行脚本按需创建以下目录：

- `releases-velopack/`：Velopack 脚本生成的安装器、更新 feed 和完整更新包；
- `release-assets/`：标签发布时生成的最终 GitHub Release 资产，包括便携版 ZIP、安装包 ZIP、版本说明、校验清单及自动更新技术文件。

`data/` 是运行时数据，不能当作构建产物清理；`tools/`、`third_party/`、`languages/` 是软件运行所需的本地组件和资源。

需要回到干净的开发工作区时执行 `scripts/organize_workspace.ps1`。该脚本会删除可重新生成的构建/发行输出、旧组件副本和已知测试缓存；保留源码、当前运行组件、数据库、设置、有效 Cookie/Profile、下载内容与三代数据库备份。
