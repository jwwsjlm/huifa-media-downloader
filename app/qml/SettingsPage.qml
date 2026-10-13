import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: root
    UiTheme { id: theme }
    property var form: ({})
    property var optionForm: ({})
    readonly property bool compactLayout: width < 900

    function reload() {
        form = Object.assign({}, appBridge.settingsModel.values)
        optionForm = Object.assign({}, appBridge.settingsModel.downloadOptions)
    }

    function textValue(key, fallback) {
        const value = form[key]
        return value === undefined || value === null ? (fallback || "") : String(value)
    }

    function boolValue(key) {
        return ["true", "1", "yes", "on"].includes(String(form[key] || "").toLowerCase())
    }

    function optionValue(key, fallback) {
        const value = optionForm[key]
        return value === undefined || value === null ? (fallback || "") : value
    }

    function setFormValue(key, value) {
        const next = Object.assign({}, form)
        next[key] = value
        form = next
    }

    function setOptionValue(key, value) {
        const next = Object.assign({}, optionForm)
        next[key] = value
        optionForm = next
    }

    function save() {
        if (appBridge.settingsModel.save(form, optionForm, openAiKey.text))
            openAiKey.clear()
    }

    Component.onCompleted: {
        appBridge.settingsModel.refresh()
        reload()
    }

    Connections {
        target: appBridge.settingsModel
        function onChanged() { root.reload() }
    }

    UiDialog {
        id: applicationInstallDialog
        parent: Overlay.overlay
        modal: true
        width: Math.min(460, root.width - 32)
        height: 230
        anchors.centerIn: parent
        title: "安装程序更新"
        standardButtons: Dialog.Ok | Dialog.Cancel
        contentItem: Text {
            width: 380
            wrapMode: Text.Wrap
            color: "#d8e7fa"
            text: "更新已下载完成。确认后程序会安全停止后台任务、退出并由更新器安装新版本。"
        }
        onAccepted: appBridge.updateModel.installApplication()
    }

    UiDialog {
        id: checksumDialog
        parent: Overlay.overlay
        modal: true
        width: Math.min(500, root.width - 32)
        height: 240
        anchors.centerIn: parent
        property string component: ""
        title: "确认无校验和下载"
        standardButtons: Dialog.Ok | Dialog.Cancel
        contentItem: Text {
            width: 420
            wrapMode: Text.Wrap
            color: "#d8e7fa"
            text: "该官方发布资源未提供 SHA-256。程序仍会校验压缩包结构和 Windows 可执行文件格式。确认继续下载并安装 " + checksumDialog.component + "？"
        }
        onAccepted: appBridge.updateModel.installRuntimeComponent(component, true)
    }

    UiDialog {
        id: cookieViewerDialog
        parent: Overlay.overlay
        modal: true
        width: Math.min(900, Overlay.overlay.width - 48)
        height: Math.min(580, Overlay.overlay.height - 48)
        anchors.centerIn: parent
        title: "内置浏览器 Cookie"
        property var entries: []
        closePolicy: Popup.CloseOnEscape

        function reload() {
            entries = appBridge.embeddedCookies(cookieSearch.text, showCookieValues.checked)
        }

        onOpened: reload()
        onClosed: {
            showCookieValues.checked = false
            cookieSearch.clear()
            entries = []
        }

        contentItem: ColumnLayout {
            spacing: 5

            Text {
                Layout.fillWidth: true
                text: "当前视图 · " + cookieViewerDialog.entries.length
                      + " 个 Cookie。值默认隐藏，不会写入日志或诊断包。"
                color: "#9eb4d1"
                wrapMode: Text.Wrap
            }

            RowLayout {
                Layout.fillWidth: true
                UiTextField {
                    id: cookieSearch
                    Layout.fillWidth: true
                    hintText: "搜索域名、Cookie 名称或路径"
                    onTextChanged: cookieViewerDialog.reload()
                }
                UiCheckBox {
                    id: showCookieValues
                    text: "显示敏感值"
                    onToggled: cookieViewerDialog.reload()
                }
                UiButton { text: "刷新"; onClicked: cookieViewerDialog.reload() }
            }

            Text {
                Layout.fillWidth: true
                visible: showCookieValues.checked
                text: "Cookie 值可能等同于登录凭据。请勿截图、复制到聊天或放入诊断包。"
                color: "#f8c577"
                wrapMode: Text.Wrap
            }

            Rectangle {
                Layout.fillWidth: true
                Layout.fillHeight: true
                radius: theme.panelRadius
                color: theme.canvasRaised
                border.color: theme.border
                clip: true

                UiFlickable {
                    id: cookieList
                    objectName: "cookieList"
                    anchors.fill: parent
                    anchors.margins: 6
                    contentWidth: width
                    contentHeight: cookieColumn.height
                    clip: true

                    Column {
                        id: cookieColumn
                        width: cookieList.width
                        height: implicitHeight
                        spacing: 7

                        Repeater {
                            model: cookieViewerDialog.entries
                            delegate: Rectangle {
                                required property var modelData
                                width: cookieColumn.width
                                implicitHeight: cookieInfo.implicitHeight + 14
                                radius: theme.panelRadius
                                color: theme.surface
                                border.color: theme.border

                                ColumnLayout {
                                    id: cookieInfo
                                    anchors.left: parent.left
                                    anchors.right: parent.right
                                    anchors.verticalCenter: parent.verticalCenter
                                    anchors.margins: 6
                                    spacing: 3
                                    Text {
                                        Layout.fillWidth: true
                                        text: modelData.domain + " · " + modelData.name
                                        color: "#e7f1ff"
                                        font.bold: true
                                        elide: Text.ElideRight
                                    }
                                    Text {
                                        Layout.fillWidth: true
                                        text: "值：" + modelData.value
                                        color: showCookieValues.checked ? "#f8c577" : "#8298b7"
                                        elide: Text.ElideRight
                                    }
                                    Text {
                                        Layout.fillWidth: true
                                        text: "路径 " + modelData.path + " · 过期 " + modelData.expires
                                              + " · Secure " + modelData.secure
                                              + " · HttpOnly " + modelData.httpOnly
                                              + " · SameSite " + modelData.sameSite
                                        color: "#7187a6"
                                        font.pixelSize: 11
                                        elide: Text.ElideRight
                                    }
                                }
                            }
                        }
                    }

                    ScrollBar.vertical: UiScrollBar { }
                }
                Text {
                    objectName: "cookieEmptyState"
                    anchors.centerIn: parent
                    visible: cookieViewerDialog.entries.length === 0
                    text: cookieSearch.text.length ? "没有匹配的 Cookie" : "内置浏览器尚未保存 Cookie"
                    color: "#7187a6"
                }
            }
            RowLayout {
                Layout.fillWidth: true
                Item { Layout.fillWidth: true }
                UiButton {
                    objectName: "cookieViewerCloseButton"
                    text: "关闭"
                    iconName: "x"
                    onClicked: cookieViewerDialog.close()
                }
            }
        }
        standardButtons: Dialog.NoButton
    }

    UiDialog {
        id: diagnosticsDialog
        parent: Overlay.overlay
        modal: true
        width: Math.min(560, root.width - 32)
        anchors.centerIn: parent
        title: "导出诊断包"
        property string exportedPath: ""
        property string status: ""
        closePolicy: Popup.CloseOnEscape

        onOpened: {
            exportedPath = ""
            status = ""
            diagnosticsPath.text = appBridge.diagnosticsDefaultPath()
        }

        contentItem: ColumnLayout {
            spacing: 12
            Text {
                Layout.fillWidth: true
                text: "导出的 ZIP 包包含运行时版本、数据库恢复状态和脱敏日志，不包含 Cookie 值或 API 密钥。"
                color: "#d8e7fa"
                wrapMode: Text.Wrap
            }
            UiTextField {
                id: diagnosticsPath
                Layout.fillWidth: true
                hintText: "诊断包 ZIP 路径"
            }
            RowLayout {
                Layout.fillWidth: true
                UiButton {
                    text: "导出"
                    highlighted: true
                    onClicked: {
                        const path = appBridge.exportDiagnostics(diagnosticsPath.text)
                        diagnosticsDialog.exportedPath = path
                        diagnosticsDialog.status = path
                            ? "已导出到：" + path
                            : appBridge.message
                    }
                }
                UiButton {
                    text: "打开所在文件夹"
                    enabled: diagnosticsDialog.exportedPath.length > 0
                    onClicked: appBridge.openDiagnosticsLocation(diagnosticsDialog.exportedPath)
                }
            }
            Text {
                Layout.fillWidth: true
                visible: diagnosticsDialog.status.length > 0
                text: diagnosticsDialog.status
                color: diagnosticsDialog.exportedPath.length > 0 ? "#9ee6b8" : "#fca5a5"
                wrapMode: Text.Wrap
            }
            RowLayout {
                Layout.fillWidth: true
                Item { Layout.fillWidth: true }
                UiButton {
                    objectName: "diagnosticsCloseButton"
                    text: "关闭"
                    iconName: "x"
                    onClicked: diagnosticsDialog.close()
                }
            }
        }
        standardButtons: Dialog.NoButton
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 5

        RowLayout {
            Layout.fillWidth: true
            Layout.alignment: Qt.AlignTop
            UiPageTitle {
                Layout.fillWidth: true
                title: "设置"
                subtitle: "下载、网络、工具和封面设置会保存到便携数据目录。"
                iconName: "settings-2"
            }
            UiButton {
                objectName: "settingsReloadButton"
                text: root.compactLayout ? "载入" : "重新载入"
                iconName: "refresh-cw"
                onClicked: appBridge.settingsModel.refresh()
            }
            UiButton {
                objectName: "settingsScanButton"
                text: root.compactLayout ? "扫描" : "扫描已有媒体"
                iconName: "search"
                onClicked: appBridge.scanLocalHistory(root.textValue("download_dir"))
            }
            UiButton {
                objectName: "settingsSaveButton"
                text: root.compactLayout ? "保存" : "保存全部设置"
                iconName: "check-circle-2"
                highlighted: true
                onClicked: root.save()
            }
        }

        TabBar {
            id: settingsTabs
            objectName: "settingsTabs"
            Layout.fillWidth: true
            implicitHeight: theme.controlHeight
            background: Rectangle {
                radius: theme.panelRadius
                color: theme.surface
                border.color: theme.border
            }
            TabButton {
                objectName: "settingsTab_download"
                text: root.compactLayout ? "下载" : "下载设置"
                contentItem: Text {
                    text: parent.text
                    color: parent.checked ? "#84edf7" : parent.hovered ? "#d8efff" : "#8fa9c8"
                    font.pixelSize: 12
                    font.weight: parent.checked ? Font.DemiBold : Font.Medium
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                    elide: Text.ElideRight
                }
                background: Rectangle {
                    radius: theme.controlRadius
                    color: parent.checked ? "#173e5b" : parent.down ? "#162c44" : parent.hovered ? "#142940" : "transparent"
                    border.width: parent.checked ? 1 : 0
                    border.color: "#2b789f"
                }
            }
            TabButton {
                objectName: "settingsTab_network"
                text: "网络"
                contentItem: Text {
                    text: parent.text
                    color: parent.checked ? "#84edf7" : parent.hovered ? "#d8efff" : "#8fa9c8"
                    font.pixelSize: 12
                    font.weight: parent.checked ? Font.DemiBold : Font.Medium
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                }
                background: Rectangle {
                    radius: theme.controlRadius
                    color: parent.checked ? "#173e5b" : parent.down ? "#162c44" : parent.hovered ? "#142940" : "transparent"
                    border.width: parent.checked ? 1 : 0
                    border.color: "#2b789f"
                }
            }
            TabButton {
                objectName: "settingsTab_tools"
                text: root.compactLayout ? "工具" : "工具与更新"
                contentItem: Text {
                    text: parent.text
                    color: parent.checked ? "#84edf7" : parent.hovered ? "#d8efff" : "#8fa9c8"
                    font.pixelSize: 12
                    font.weight: parent.checked ? Font.DemiBold : Font.Medium
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                    elide: Text.ElideRight
                }
                background: Rectangle {
                    radius: theme.controlRadius
                    color: parent.checked ? "#173e5b" : parent.down ? "#162c44" : parent.hovered ? "#142940" : "transparent"
                    border.width: parent.checked ? 1 : 0
                    border.color: "#2b789f"
                }
            }
            TabButton {
                objectName: "settingsTab_cover"
                text: root.compactLayout ? "封面" : "外观与封面"
                contentItem: Text {
                    text: parent.text
                    color: parent.checked ? "#84edf7" : parent.hovered ? "#d8efff" : "#8fa9c8"
                    font.pixelSize: 12
                    font.weight: parent.checked ? Font.DemiBold : Font.Medium
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                    elide: Text.ElideRight
                }
                background: Rectangle {
                    radius: theme.controlRadius
                    color: parent.checked ? "#173e5b" : parent.down ? "#162c44" : parent.hovered ? "#142940" : "transparent"
                    border.width: parent.checked ? 1 : 0
                    border.color: "#2b789f"
                }
            }
        }

        StackLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            currentIndex: settingsTabs.currentIndex

            ScrollView {
                objectName: "settingsDownloadScroll"
                clip: true
                contentWidth: availableWidth
                ScrollBar.vertical: UiScrollBar { }
                ScrollBar.horizontal: UiScrollBar {
                    objectName: "settingsDownloadHorizontalScrollBar"
                    policy: ScrollBar.AlwaysOff
                }

                ColumnLayout {
                    width: parent.width
                    spacing: 5

                    Rectangle {
                        Layout.fillWidth: true
                        implicitHeight: downloadGrid.implicitHeight + 12
                        radius: theme.panelRadius
                        color: theme.surface
                        border.color: theme.border
                        GridLayout {
                            id: downloadGrid
                            anchors.fill: parent
                            anchors.margins: 6
                            columns: root.compactLayout ? 1 : 2
                            columnSpacing: 5
                            rowSpacing: 5
                            Text { text: "下载目录"; color: "#bcd3ef" }
                            UiTextField {
                                Layout.fillWidth: true
                                text: root.textValue("download_dir")
                                hintText: "选择或输入下载目录"
                                onTextEdited: root.setFormValue("download_dir", text)
                            }
                            Text { text: "处理临时目录"; color: "#bcd3ef" }
                            UiTextField {
                                Layout.fillWidth: true
                                text: root.textValue("processing_temp_dir")
                                hintText: "留空则在最终目录处理"
                                onTextEdited: root.setFormValue("processing_temp_dir", text)
                            }
                            Text { text: "文件名模板"; color: "#bcd3ef" }
                            UiTextField {
                                Layout.fillWidth: true
                                text: root.textValue("filename_template")
                                onTextEdited: root.setFormValue("filename_template", text)
                            }
                            Text { text: "任务独立文件夹"; color: "#bcd3ef" }
                            UiCheckBox {
                                checked: root.boolValue("organize_task_folder")
                                text: "为每个任务创建文件夹"
                                onToggled: root.setFormValue("organize_task_folder", checked)
                            }
                            Text { text: "默认清晰度"; color: "#bcd3ef" }
                            UiComboBox {
                                id: quality
                                Layout.fillWidth: true
                                model: ["best", "8k", "4k", "2k", "1080p", "720p", "480p", "360p", "240p"]
                                currentIndex: Math.max(0, model.indexOf(root.textValue("quality", "best")))
                                onActivated: root.setFormValue("quality", currentText)
                            }
                            Text { text: "内容类型"; color: "#bcd3ef" }
                            UiComboBox {
                                Layout.fillWidth: true
                                model: ["manual", "video", "audio"]
                                currentIndex: Math.max(0, model.indexOf(String(root.optionValue("content_mode", "video"))))
                                onActivated: root.setOptionValue("content_mode", currentText)
                            }
                            Text { text: "视频容器"; color: "#bcd3ef" }
                            UiComboBox {
                                Layout.fillWidth: true
                                model: ["auto", "mp4", "mkv"]
                                currentIndex: Math.max(0, model.indexOf(String(root.optionValue("container", "auto"))))
                                onActivated: root.setOptionValue("container", currentText)
                            }
                            Text { text: "音频格式"; color: "#bcd3ef" }
                            UiComboBox {
                                Layout.fillWidth: true
                                model: ["best", "aac", "alac", "flac", "m4a", "mp3", "opus", "vorbis", "wav"]
                                currentIndex: Math.max(0, model.indexOf(String(root.optionValue("audio_format", "best"))))
                                onActivated: root.setOptionValue("audio_format", currentText)
                            }
                            Text { text: "兼容设备"; color: "#bcd3ef" }
                            UiComboBox {
                                Layout.fillWidth: true
                                model: ["auto", "windows", "macos", "linux", "ios", "android"]
                                currentIndex: Math.max(0, model.indexOf(String(root.optionValue("compatibility_target", "auto"))))
                                onActivated: root.setOptionValue("compatibility_target", currentText)
                            }
                            Text { text: "视频编码偏好"; color: "#bcd3ef" }
                            UiComboBox {
                                Layout.fillWidth: true
                                model: ["auto", "h264", "h265", "av1", "vp9"]
                                currentIndex: Math.max(0, model.indexOf(String(root.optionValue("source_video_codec", "auto"))))
                                onActivated: root.setOptionValue("source_video_codec", currentText)
                            }
                            Text { text: "字幕语言"; color: "#bcd3ef" }
                            UiTextField {
                                Layout.fillWidth: true
                                text: root.textValue("subtitle_language", "none")
                                hintText: "none、zh-Hans、en …"
                                onTextEdited: root.setFormValue("subtitle_language", text)
                            }
                            Text { text: "链接解析"; color: "#bcd3ef" }
                            UiComboBox {
                                objectName: "settingsPlaylistMode"
                                Layout.fillWidth: true
                                model: [
                                    { label: "智能识别（推荐）", value: "auto" },
                                    { label: "仅下载当前视频", value: "single" },
                                    { label: "始终按合集解析", value: "playlist" }
                                ]
                                textRole: "label"
                                valueRole: "value"
                                currentIndex: root.textValue("playlist_mode", "auto") === "single" ? 1
                                    : root.textValue("playlist_mode", "auto") === "playlist" ? 2 : 0
                                onActivated: root.setFormValue("playlist_mode", currentValue)
                            }
                            Text { text: "合集处理"; color: "#bcd3ef" }
                            UiComboBox {
                                objectName: "settingsCollectionMode"
                                Layout.fillWidth: true
                                enabled: root.textValue("playlist_mode", "auto") !== "single"
                                model: [
                                    { label: "解析后选择（推荐）", value: "select" },
                                    { label: "仅下载当前条目", value: "single" },
                                    { label: "直接下载全部", value: "all" }
                                ]
                                textRole: "label"
                                valueRole: "value"
                                currentIndex: root.optionValue("collection_mode", "select") === "single" ? 1
                                    : root.optionValue("collection_mode", "select") === "all" ? 2 : 0
                                onActivated: root.setOptionValue("collection_mode", currentValue)
                            }
                            Text { text: "视频编码器"; color: "#bcd3ef" }
                            UiComboBox {
                                Layout.fillWidth: true
                                model: ["original", "libx264", "libx265", "h264_nvenc", "hevc_nvenc", "h264_qsv", "hevc_qsv", "h264_amf", "hevc_amf"]
                                currentIndex: Math.max(0, model.indexOf(root.textValue("transcode_encoder", "original")))
                                onActivated: root.setFormValue("transcode_encoder", currentText)
                            }
                        }
                    }

                    Rectangle {
                        Layout.fillWidth: true
                        implicitHeight: performanceGrid.implicitHeight + 12
                        radius: theme.panelRadius
                        color: theme.surface
                        border.color: theme.border
                        GridLayout {
                            id: performanceGrid
                            anchors.fill: parent
                            anchors.margins: 6
                            columns: root.compactLayout ? 1 : 2
                            columnSpacing: 5
                            rowSpacing: 5
                            Text { text: "性能模式"; color: "#bcd3ef" }
                            UiComboBox {
                                Layout.fillWidth: true
                                model: ["smart", "manual"]
                                currentIndex: Math.max(0, model.indexOf(root.textValue("download_performance_mode", "smart")))
                                onActivated: root.setFormValue("download_performance_mode", currentText)
                            }
                            Text { text: "并发任务"; color: "#bcd3ef" }
                            UiSpinBox {
                                objectName: "settingsMaxConcurrentSpinBox"
                                Layout.fillWidth: true
                                enabled: root.textValue("download_performance_mode", "smart") === "manual"
                                from: 1; to: 8
                                value: Number(root.textValue("max_concurrent", "3"))
                                onValueModified: root.setFormValue("max_concurrent", value)
                            }
                            Text { text: "分片并发"; color: "#bcd3ef" }
                            UiSpinBox {
                                Layout.fillWidth: true
                                enabled: root.textValue("download_performance_mode", "smart") === "manual"
                                from: 1; to: 32
                                value: Number(root.textValue("fragment_concurrent", "12"))
                                onValueModified: root.setFormValue("fragment_concurrent", value)
                            }
                            Text { text: "请求延迟（秒）"; color: "#bcd3ef" }
                            UiTextField {
                                Layout.fillWidth: true
                                enabled: root.textValue("download_performance_mode", "smart") === "manual"
                                text: root.textValue("request_delay", "0")
                                inputMethodHints: Qt.ImhFormattedNumbersOnly
                                onTextEdited: root.setFormValue("request_delay", text)
                            }
                        }
                    }
                    Item { Layout.preferredHeight: 8 }
                }
            }

            ScrollView {
                clip: true
                contentWidth: availableWidth
                ScrollBar.vertical: UiScrollBar { }
                ScrollBar.horizontal: UiScrollBar { policy: ScrollBar.AlwaysOff }

                ColumnLayout {
                    width: parent.width
                    spacing: 5
                    Rectangle {
                        Layout.fillWidth: true
                        implicitHeight: networkGrid.implicitHeight + 12
                        radius: theme.panelRadius
                        color: theme.surface
                        border.color: theme.border
                        GridLayout {
                            id: networkGrid
                            anchors.fill: parent
                            anchors.margins: 6
                            columns: root.compactLayout ? 1 : 2
                            columnSpacing: 5
                            rowSpacing: 5
                            Text { text: "下载代理"; color: "#bcd3ef" }
                            UiTextField { Layout.fillWidth: true; text: root.textValue("proxy"); hintText: "http://127.0.0.1:7890"; onTextEdited: root.setFormValue("proxy", text) }
                            Text { text: "Cookie 来源"; color: "#bcd3ef" }
                            UiComboBox {
                                Layout.fillWidth: true
                                model: ["none", "embedded", "browser", "file"]
                                currentIndex: Math.max(0, model.indexOf(root.textValue("download_cookie_source", "none")))
                                onActivated: root.setFormValue("download_cookie_source", currentText)
                            }
                            Text { text: "Cookie 文件"; color: "#bcd3ef" }
                            UiTextField { Layout.fillWidth: true; enabled: root.textValue("download_cookie_source", "none") === "file"; text: root.textValue("download_cookie_file"); hintText: "Netscape Cookie 文件路径"; onTextEdited: root.setFormValue("download_cookie_file", text) }
                            Text { text: "浏览器"; color: "#bcd3ef" }
                            UiComboBox {
                                Layout.fillWidth: true
                                enabled: root.textValue("download_cookie_source", "none") === "browser"
                                model: ["chrome", "edge", "firefox", "brave"]
                                currentIndex: Math.max(0, model.indexOf(root.textValue("download_cookie_browser", "chrome")))
                                onActivated: root.setFormValue("download_cookie_browser", currentText)
                            }
                            Text { text: "浏览器配置文件"; color: "#bcd3ef" }
                            UiTextField { Layout.fillWidth: true; enabled: root.textValue("download_cookie_source", "none") === "browser"; text: root.textValue("download_cookie_profile"); onTextEdited: root.setFormValue("download_cookie_profile", text) }
                            Text { text: "钥匙串 / 容器"; color: "#bcd3ef" }
                            RowLayout {
                                Layout.fillWidth: true
                                UiTextField { Layout.fillWidth: true; enabled: root.textValue("download_cookie_source", "none") === "browser"; text: root.textValue("download_cookie_keyring"); hintText: "钥匙串"; onTextEdited: root.setFormValue("download_cookie_keyring", text) }
                                UiTextField { Layout.fillWidth: true; enabled: root.textValue("download_cookie_source", "none") === "browser"; text: root.textValue("download_cookie_container"); hintText: "Firefox 容器"; onTextEdited: root.setFormValue("download_cookie_container", text) }
                            }
                            Text { text: "Cookie 操作"; color: "#bcd3ef" }
                            RowLayout {
                                Layout.fillWidth: true
                                UiButton {
                                    text: "检查 Cookie"
                                    onClicked: appBridge.checkDownloadCookies()
                                }
                                UiButton {
                                    text: "打开内置登录"
                                    onClicked: appBridge.openDownloadCookieLogin()
                                }
                                UiButton {
                                    text: "查看内置 Cookie"
                                    onClicked: cookieViewerDialog.open()
                                }
                                Text {
                                    Layout.fillWidth: true
                                    text: "先保存设置，再检查或登录"
                                    color: "#8298b7"
                                    font.pixelSize: 11
                                }
                            }
                        }
                    }
                    Item { Layout.preferredHeight: 8 }
                }
            }

            ScrollView {
                clip: true
                contentWidth: availableWidth
                ScrollBar.vertical: UiScrollBar { }
                ScrollBar.horizontal: UiScrollBar { policy: ScrollBar.AlwaysOff }

                ColumnLayout {
                    width: parent.width
                    spacing: 5
                    Rectangle {
                        Layout.fillWidth: true
                        implicitHeight: toolsGrid.implicitHeight + 12
                        radius: theme.panelRadius
                        color: theme.surface
                        border.color: theme.border
                        GridLayout {
                            id: toolsGrid
                            anchors.fill: parent
                            anchors.margins: 6
                            columns: root.compactLayout ? 1 : 2
                            columnSpacing: 5
                            rowSpacing: 5
                            Text { text: "FFmpeg 构建"; color: "#bcd3ef" }
                            UiComboBox {
                                Layout.fillWidth: true
                                model: ["nvenc_13_0", "latest"]
                                currentIndex: Math.max(0, model.indexOf(root.textValue("ffmpeg_build_channel", "nvenc_13_0")))
                                onActivated: root.setFormValue("ffmpeg_build_channel", currentText)
                            }
                            Text { text: "FFmpeg 路径"; color: "#bcd3ef" }
                            UiTextField { Layout.fillWidth: true; text: root.textValue("ffmpeg_path"); hintText: "留空使用内置兼容版"; onTextEdited: root.setFormValue("ffmpeg_path", text) }
                            Text { text: "FFprobe 路径"; color: "#bcd3ef" }
                            UiTextField { Layout.fillWidth: true; text: root.textValue("ffprobe_path"); onTextEdited: root.setFormValue("ffprobe_path", text) }
                            Text { text: "Deno 路径"; color: "#bcd3ef" }
                            UiTextField { Layout.fillWidth: true; text: root.textValue("deno_path"); onTextEdited: root.setFormValue("deno_path", text) }
                            Text { text: "yt-dlp 核心"; color: "#bcd3ef" }
                            UiComboBox {
                                Layout.fillWidth: true
                                model: ["auto", "external", "builtin"]
                                currentIndex: Math.max(0, model.indexOf(root.textValue("ytdlp_core_mode", "auto")))
                                onActivated: root.setFormValue("ytdlp_core_mode", currentText)
                            }
                            Text { text: "yt-dlp-ejs 来源"; color: "#bcd3ef" }
                            UiComboBox {
                                Layout.fillWidth: true
                                model: ["auto", "npm", "github", "local"]
                                currentIndex: Math.max(0, model.indexOf(root.textValue("ytdlp_ejs_source", "auto")))
                                onActivated: root.setFormValue("ytdlp_ejs_source", currentText)
                            }
                        }
                    }

                    Rectangle {
                        Layout.fillWidth: true
                        implicitHeight: updateGrid.implicitHeight + 12
                        radius: theme.panelRadius
                        color: theme.surface
                        border.color: theme.border
                        GridLayout {
                            id: updateGrid
                            anchors.fill: parent
                            anchors.margins: 6
                            columns: root.compactLayout ? 1 : 2
                            columnSpacing: 5
                            rowSpacing: 5
                            Text { text: "GitHub 下载线路"; color: "#bcd3ef" }
                            UiTextField { Layout.fillWidth: true; text: root.textValue("github_download_route", "auto"); hintText: "auto 或 direct"; onTextEdited: root.setFormValue("github_download_route", text) }
                            Text { text: "自定义镜像"; color: "#bcd3ef"; Layout.alignment: Qt.AlignTop }
                            UiTextArea { Layout.fillWidth: true; Layout.preferredHeight: 76; text: root.textValue("github_mirror_urls"); hintText: "每行一个 HTTPS 镜像地址"; onTextChanged: if (activeFocus) root.setFormValue("github_mirror_urls", text) }
                            Text { text: "自动检查应用更新"; color: "#bcd3ef" }
                            UiCheckBox { checked: root.boolValue("auto_check_updates"); text: "启动后自动检查"; onToggled: root.setFormValue("auto_check_updates", checked) }
                            Text { text: "包含预发布版本"; color: "#bcd3ef" }
                            UiCheckBox { checked: root.boolValue("update_prerelease"); text: "允许预发布版本"; onToggled: root.setFormValue("update_prerelease", checked) }
                        }
                    }

                    Rectangle {
                        Layout.fillWidth: true
                        implicitHeight: updatePanel.implicitHeight + 12
                        radius: theme.panelRadius
                        color: theme.surface
                        border.color: theme.border

                        ColumnLayout {
                            id: updatePanel
                            anchors.fill: parent
                            anchors.margins: 6
                            spacing: 5

                            RowLayout {
                                Layout.fillWidth: true
                                Text {
                                    Layout.fillWidth: true
                                    text: "程序更新"
                                    color: "#e7f1ff"
                                    font.pixelSize: 15
                                    font.bold: true
                                }
                                UiButton {
                                    text: "检查程序更新"
                                    enabled: appBridge.updateModel.applicationSupported
                                             && !appBridge.updateModel.applicationBusy
                                    onClicked: appBridge.updateModel.checkApplication()
                                }
                            }

                            Text {
                                Layout.fillWidth: true
                                text: appBridge.updateModel.applicationStatus
                                color: "#9eb4d1"
                                wrapMode: Text.Wrap
                            }

                            UiProgressBar {
                                Layout.fillWidth: true
                                visible: appBridge.updateModel.applicationBusy
                                         || appBridge.updateModel.applicationProgress > 0
                                from: 0
                                to: 100
                                value: appBridge.updateModel.applicationProgress
                            }

                            RowLayout {
                                Layout.fillWidth: true
                                visible: appBridge.updateModel.applicationUpdate.version !== undefined
                                Text {
                                    Layout.fillWidth: true
                                    text: "可用版本 " + (appBridge.updateModel.applicationUpdate.version || "")
                                    color: "#bcd3ef"
                                }
                                UiButton {
                                    text: appBridge.updateModel.applicationUpdate.downloaded ? "重启并安装" : "下载更新"
                                    enabled: !appBridge.updateModel.applicationBusy
                                    onClicked: {
                                        if (appBridge.updateModel.applicationUpdate.downloaded)
                                            applicationInstallDialog.open()
                                        else
                                            appBridge.updateModel.downloadApplication()
                                    }
                                }
                                UiButton {
                                    text: "暂停"
                                    visible: appBridge.updateModel.applicationBusy
                                             && appBridge.updateModel.applicationProgress > 0
                                             && appBridge.updateModel.applicationProgress < 100
                                    onClicked: appBridge.updateModel.cancelApplicationDownload()
                                }
                            }

                            Rectangle {
                                Layout.fillWidth: true
                                Layout.preferredHeight: 1
                                color: "#263755"
                            }

                            RowLayout {
                                Layout.fillWidth: true
                                Text {
                                    Layout.fillWidth: true
                                    text: "运行时组件"
                                    color: "#e7f1ff"
                                    font.pixelSize: 15
                                    font.bold: true
                                }
                                UiButton {
                                    text: appBridge.updateModel.installingComponent.length > 0 ? "安装中…"
                                          : appBridge.updateModel.runtimeBusy ? "检查中…" : "检查组件更新"
                                    enabled: !appBridge.updateModel.runtimeBusy
                                    onClicked: appBridge.updateModel.checkRuntimeComponents()
                                }
                            }

                            Text {
                                Layout.fillWidth: true
                                text: appBridge.updateModel.runtimeStatus
                                color: "#9eb4d1"
                                wrapMode: Text.Wrap
                            }

                            Repeater {
                                model: appBridge.updateModel.runtimeResults
                                delegate: Rectangle {
                                    required property var modelData
                                    Layout.fillWidth: true
                                    implicitHeight: componentRow.implicitHeight + 18
                                    radius: theme.panelRadius
                                    color: theme.canvasRaised
                                    border.color: theme.border

                                    RowLayout {
                                        id: componentRow
                                        anchors.fill: parent
                                        anchors.margins: 9
                                        spacing: 5
                                        ColumnLayout {
                                            Layout.fillWidth: true
                                            spacing: 2
                                            Text {
                                                Layout.fillWidth: true
                                                text: modelData.name + " · " + (modelData.current || "未检测")
                                                color: "#dceaff"
                                                elide: Text.ElideRight
                                            }
                                            Text {
                                                Layout.fillWidth: true
                                                text: modelData.error || ("远程版本 " + (modelData.latest || "—"))
                                                color: modelData.error ? "#fca5a5" : "#8298b7"
                                                font.pixelSize: 11
                                                elide: Text.ElideRight
                                            }
                                        }
                                        UiButton {
                                            text: modelData.installing ? "安装中…"
                                                  : modelData.install_available ? "安装"
                                                  : modelData.has_update ? "更新" : "已是最新"
                                            enabled: modelData.actionable && !appBridge.updateModel.runtimeBusy
                                            onClicked: {
                                                if (appBridge.updateModel.componentNeedsChecksumConfirmation(modelData.name)) {
                                                    checksumDialog.component = modelData.name
                                                    checksumDialog.open()
                                                } else {
                                                    appBridge.updateModel.installRuntimeComponent(modelData.name, false)
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                    Item { Layout.preferredHeight: 8 }
                }
            }

            ScrollView {
                clip: true
                contentWidth: availableWidth
                ScrollBar.vertical: UiScrollBar { }
                ScrollBar.horizontal: UiScrollBar { policy: ScrollBar.AlwaysOff }

                ColumnLayout {
                    width: parent.width
                    spacing: 5
                    Rectangle {
                        Layout.fillWidth: true
                        implicitHeight: appearanceGrid.implicitHeight + 12
                        radius: theme.panelRadius
                        color: theme.surface
                        border.color: theme.border
                        GridLayout {
                            id: appearanceGrid
                            anchors.fill: parent
                            anchors.margins: 6
                            columns: root.compactLayout ? 1 : 2
                            columnSpacing: 5
                            rowSpacing: 5
                            Text { text: "界面外观"; color: "#bcd3ef" }
                            Text {
                                Layout.fillWidth: true
                                text: "深色高对比界面会随窗口宽度自动调整布局。"
                                color: "#829cba"
                                font.pixelSize: 12
                                wrapMode: Text.WordWrap
                            }
                            Text { text: "桌面通知"; color: "#bcd3ef" }
                            UiCheckBox { checked: root.boolValue("desktop_notifications"); text: "下载和发布完成时通知"; onToggled: root.setFormValue("desktop_notifications", checked) }
                        }
                    }

                    Rectangle {
                        Layout.fillWidth: true
                        implicitHeight: coverGrid.implicitHeight + 12
                        radius: theme.panelRadius
                        color: theme.surface
                        border.color: theme.border
                        GridLayout {
                            id: coverGrid
                            anchors.fill: parent
                            anchors.margins: 6
                            columns: root.compactLayout ? 1 : 2
                            columnSpacing: 5
                            rowSpacing: 5
                            Text { text: "封面预设"; color: "#bcd3ef" }
                            UiTextField { Layout.fillWidth: true; text: root.textValue("cover_preset", "landscape_16_9"); onTextEdited: root.setFormValue("cover_preset", text) }
                            Text { text: "封面适配"; color: "#bcd3ef" }
                            UiComboBox { Layout.fillWidth: true; model: ["crop", "pad"]; currentIndex: Math.max(0, model.indexOf(root.textValue("cover_fit_mode", "crop"))); onActivated: root.setFormValue("cover_fit_mode", currentText) }
                            Text { text: "裁切焦点 X / Y"; color: "#bcd3ef" }
                            RowLayout {
                                Layout.fillWidth: true
                                UiSpinBox { Layout.fillWidth: true; from: 0; to: 100; value: Number(root.textValue("cover_focus_x", "50")); onValueModified: root.setFormValue("cover_focus_x", value) }
                                UiSpinBox { Layout.fillWidth: true; from: 0; to: 100; value: Number(root.textValue("cover_focus_y", "50")); onValueModified: root.setFormValue("cover_focus_y", value) }
                            }
                            Text { text: "下载封面转 JPG"; color: "#bcd3ef" }
                            UiCheckBox { checked: root.boolValue("download_cover_convert_jpeg"); text: "自动转换"; onToggled: root.setFormValue("download_cover_convert_jpeg", checked) }
                            Text { text: "JPG 质量"; color: "#bcd3ef" }
                            UiSpinBox { Layout.fillWidth: true; from: 50; to: 100; value: Number(root.textValue("cover_jpeg_quality", "90")); onValueModified: root.setFormValue("cover_jpeg_quality", value) }
                            Text { text: "片头封面"; color: "#bcd3ef" }
                            RowLayout {
                                Layout.fillWidth: true
                                UiCheckBox { checked: root.boolValue("prepend_cover_enabled"); text: "插入下载封面"; onToggled: root.setFormValue("prepend_cover_enabled", checked) }
                                UiSpinBox { enabled: root.boolValue("prepend_cover_enabled"); from: 1; to: 300; value: Number(root.textValue("prepend_cover_frames", "3")); onValueModified: root.setFormValue("prepend_cover_frames", value) }
                            }
                            Text { text: "GPT Image 模型"; color: "#bcd3ef" }
                            UiTextField { Layout.fillWidth: true; text: root.textValue("cover_ai_model", "gpt-image-2"); onTextEdited: root.setFormValue("cover_ai_model", text) }
                            Text { text: "图像 API URL"; color: "#bcd3ef" }
                            UiTextField { Layout.fillWidth: true; text: root.textValue("cover_ai_api_url"); hintText: "留空使用 OpenAI"; onTextEdited: root.setFormValue("cover_ai_api_url", text) }
                            Text { text: "OpenAI API 密钥"; color: "#bcd3ef" }
                            RowLayout {
                                Layout.fillWidth: true
                                UiTextField { id: openAiKey; Layout.fillWidth: true; echoMode: TextInput.Password; hintText: appBridge.settingsModel.hasOpenAiKey ? "已安全保存；留空则不变" : "输入后保存到系统凭据库" }
                                UiButton { text: "清除密钥"; onClicked: appBridge.settingsModel.clearOpenAiKey() }
                            }
                        }
                    }

                    Rectangle {
                        Layout.fillWidth: true
                        implicitHeight: supportGrid.implicitHeight + 12
                        radius: theme.panelRadius
                        color: theme.surface
                        border.color: theme.border
                        GridLayout {
                            id: supportGrid
                            anchors.fill: parent
                            anchors.margins: 6
                            columns: root.compactLayout ? 1 : 2
                            columnSpacing: 5
                            rowSpacing: 5
                            Text { text: "日志与诊断"; color: "#bcd3ef" }
                            RowLayout {
                                Layout.fillWidth: true
                                UiButton { text: "打开日志目录"; onClicked: appBridge.openLogDirectory() }
                                UiButton { text: "导出诊断包"; onClicked: diagnosticsDialog.open() }
                            }
                            Text { text: "语言包"; color: "#bcd3ef" }
                            UiButton {
                                text: "打开语言包目录"
                                onClicked: appBridge.openLanguagePackDirectory()
                            }
                        }
                    }
                    Item { Layout.preferredHeight: 8 }
                }
            }
        }
    }
}
