# QML 界面审查进度

## 范围与约束

- 目标：检查 bug、修复错位，统一简洁扁平化风格。
- 保留完整 QML Repeater，不改成虚拟任务列表。
- 未获明确授权前，不执行测试、启动程序、构建、打包或发布。
- 当前状态：已修复一批源码级问题；整体目标仍在进行中，未完成运行时视觉验收。

## 2026-09-13：共享控件、弹窗与布局

### 已落实

1. **文字按钮边框过紧**
   - 本地 Qt Material `Button.qml` 为默认背景设置了上下各 6px inset。
   - 自定义 UiButton 只覆盖高度和 padding，仍继承 inset；32px 控件的可见背景因此只有 20px。
   - 清零四边 inset，保留 16px 文字水平留白、6px 垂直留白。
   - 内容行按可用宽度收缩并省略长文字，背景与点击区域一致。

2. **数字框、复选框、单选框文字/指示器错位**
   - UiSpinBox 清除 Material 上下 padding，移除编辑器重复的左右 padding。
   - 上下箭头采用整数坐标，保持图标在各自区域内。
   - 选择控件明确垂直 padding；无文字时使用方形点击区域，指示器居中。
   - 下拉选项明确上下 padding，避免继承适用于大尺寸控件的留白。

3. **右键菜单定位与风格**
   - 编辑框把共享菜单直接赋给 `ContextMenu.menu`，由 Qt 使用当前事件位置和编辑框
     作为父项定位，不再手动弹出或换算坐标；编辑菜单使用 Qt Windows 编辑菜单的
     `Popup.Window` 模式。
   - 任务菜单仍显式调用 `popup(parent, x, y)`，坐标取自当前卡片的局部右键位置，
     并使用 `Popup.Item` 置于应用 Overlay。位置仍待不同 DPI 下验收。
   - 编辑菜单宽 132px、菜单项高 28px；任务菜单宽 186px，菜单项高 30px，分隔线高 7px；
     不用 anchors 强行覆盖 MenuItem 的 contentItem 几何，避免 Qt 控件布局与 QML
     anchors 互相抢写导致文字再次错位。
   - 手工创建的任务菜单项也使用共享 UiMenuItem，不能只修改 Menu.delegate。

4. **主页与任务卡**
   - 未激活的 secondaryPageLoader 同时设为不可见，避免仍参与 RowLayout 分宽。
   - 导航图标固定 24px 槽位，选中图标保持 24px；标签不随选中状态改变字号。
   - 长任务状态限制宽度并省略；悬停提示保留槽位，避免进度区在悬停时移动。
   - 保留展开、滚动回弹和箭头旋转；不缩放整张卡片或按钮的点击区域。
   - 下载、已完成、发布队列卡片按实际内容高度展开，Loader 使用内容隐式高度，
     按钮换行时卡片同步增高，不再挤入下一张卡片。
   - 任务卡右键只由右键释放事件打开，长按路径仅处理左键，避免 Windows 上同一次
     右键同时触发两次菜单。
   - 内容布局不依赖正在动画的卡片高度，卡片仅负责裁切，保留展开动画。
   - 主工具栏与媒体/发布操作按钮移除重复高度覆盖，沿用共享按钮的内容加内边距计算。
   - 底部汇总和提示设置宽度上限及省略，截断时悬停显示完整文字。

5. **其他页面**
   - 账号页根据实际内容宽度切换布局；行高随控件计算，表头与操作列对齐。
   - 发布编辑器、封面工作室统一使用 UiDialog 的扁平背景和紧凑标题。
   - 通用确认弹窗的标准按钮使用 UiButton。
   - 日志改为可滚动并自动折行，补齐 TaskLog 的本地 theme 引用。
   - 滚动条改用 Basic 基础，避免 Material 淡出 transition 覆盖自定义透明度；
     菜单分隔线也统一使用 Basic 基础。
   - 进度角色通知补回下载过程中可能变化的标题、转码状态和活动标识，避免漏刷。
   - 任务模型在进度引起状态/标题排序变化时使用 `beginMoveRows`，避免卡片被
     remove/insert 重建而丢失展开状态；移动被 Qt 拒绝时回退到权威 reset。
   - 合集筛选改为让 SQLite 返回完整的匹配结果集；QML `Repeater` 只保留匹配卡片，
     不再把未匹配的全部卡片、封面请求和隐藏绑定留在界面树中。每个模型角色按需
     返回，避免一次角色读取重复构造整行字典。
   - 启动任务恢复和合集恢复增加异常处理，错误显示在状态栏；
     加入合集失败不会提前设置“已确认”。这不证明恢复异常是 QML 空模型的根因。

### 验证边界

- 已核对安装环境中的 Qt QML 样式源码和 plugins.qmltypes。
- `git diff --check` 通过；未跟踪的 50 个 QML/presentation 文件也逐一进行
  `git diff --no-index --check`，未发现空白错误。
- `--no-index` 的退出码 1 代表文件有差异，不等同于空白错误；实际以诊断输出为依据。
- 补充卡片自适应高度和状态栏约束后，再次检查已跟踪差异及本次涉及的 11 个
  未跟踪文件，未发现空白错误；仅有 3 个既有文件的 LF→CRLF 提示。
- 未进行 QML lint、功能测试、程序启动或截图验收；不能据此宣称视觉与运行时全部通过。

### 截图问题相关文件

- `app/qml/UiTextField.qml`、`app/qml/UiTextArea.qml`：编辑菜单坐标。
- `app/qml/UiEditContextMenu.qml`、`app/qml/UiMenuItem.qml`、
  `app/qml/UiMenuSeparator.qml`：菜单尺寸和扁平外观。
- `app/qml/UiButton.qml`：按钮背景边界和文字内边距。
- `app/qml/TaskCard.qml`：任务菜单坐标、尺寸和操作区自适应高度。
- `app/presentation/collection_entry_model.py`、`app/qml/CollectionSelection.qml`：合集筛选和封面卡片数量。
- `app/qml/CompletedPage.qml`、`app/qml/PublishQueuePage.qml`：操作区自适应高度。
- `app/qml/Main.qml`：工具栏按钮沿用共享高度、状态栏宽度约束。

## 后续检查

- 获得用户明确授权后，验收 640/920/1120px 窗口与不同 DPI 下的菜单、导航、
  长标题、输入框、账号页、对话框和长日志。
- 待运行时确认展开卡片的按钮换行与高度动画、窄窗口状态栏省略及提示。
- 下载、格式与账号模型的角色读取已改为按请求角色返回，不再为单个绑定构造整行字典。
- 异步数据库/下载服务的底层错误仍依赖现有日志，尚未做运行时截图验收。

## 2026-09-18：Motrix 对照与 NSIS 安装向导

- Motrix 当前采用 Electron + Vue 2 + Vuex + Element UI，发布由 electron-builder
  生成 NSIS 安装包；Windows 配置为 `oneClick: false` 和
  `allowToChangeInstallationDirectory: true`，因此安装向导显式允许改目录。
- 已从 Inno 外层向导切换到 NSIS Modern UI 2，欢迎、目录、安装、完成四页明确声明，
  默认目录为 `$PROGRAMFILES64\HuifaMediaDownloader`，保留中文和英文向导。
- 本项目是 Qt/QML 应用，直接使用 NSIS 编译器，不引入 Electron/electron-builder。
  内部仍调用 Velopack MSI，传递所选路径，由 MSI 负责文件、卸载注册与升级登记；
  NSIS 不创建第二个卸载入口。失败会展示 MSI 错误码后直接退出，需重启返回码单独处理。
- 已核对 Velopack 官方安装器文档：`VELOPACK_INSTALLDIR` 是 MSI 的安全目录覆盖属性，
  与当前 NSIS 调用一致；`--instLocation Either` 保留 MSI 的双范围安装能力。
- 构建脚本、tag 工作流和资产封装已切换到 NSIS 3.12；移除旧 `.iss` 文件。
- `.gitignore` 已改为放行新的 `.nsi` 文件，避免 tag 提交遗漏安装脚本。
- 原有程序启动及本地 apply 烟雾检查改为显式 `-RunSmokeChecks` 开启；默认构建
  也不再单独运行 `compileall`。
  默认构建只编译和检查包结构、校验和，不隐式启动应用。
- 留下一个可手动运行的 NSIS 编译检查 `tests/check_nsis_installer.py`，
  覆盖中文/空格源路径与真实 `.nsi` 编译；尚未运行，也不代替真实安装验收。
- 尚待编译与安装验收：目录页、路径传递、旧版本升级、卸载入口、自更新和数据保留。
- 对照来源：Motrix 仓库 `package.json`、`electron-builder.json`；
  NSIS 官方 Modern UI 2/Scripting Reference；本地 Velopack 构建与打包脚本。
