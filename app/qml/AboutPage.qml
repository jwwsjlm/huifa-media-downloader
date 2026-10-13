import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: root
    UiTheme { id: theme }

    UiDialog {
        id: sitesDialog
        objectName: "supportedSitesDialog"
        parent: Overlay.overlay
        modal: true
        focus: true
        title: "yt-dlp 支持的网站"
        width: Math.min(580, Overlay.overlay.width - 40)
        height: Math.min(640, Overlay.overlay.height - 40)
        anchors.centerIn: parent
        standardButtons: Dialog.NoButton
        closePolicy: Popup.CloseOnEscape
        padding: 10

        header: Item {
            implicitHeight: 42

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 12
                anchors.rightMargin: 12
                spacing: 7

                Rectangle {
                    Layout.preferredWidth: 24
                    Layout.preferredHeight: 24
                    radius: theme.controlRadius
                    color: theme.primarySurface
                    LucideIcon {
                        anchors.centerIn: parent
                        name: "external-link"
                        iconSize: 14
                        color: "#7de7f2"
                    }
                }
                Text {
                    Layout.fillWidth: true
                    text: sitesDialog.title
                    color: "#eff7ff"
                    font.pixelSize: 14
                    font.weight: Font.DemiBold
                }
                UiButton {
                    objectName: "supportedSitesCloseButton"
                    text: "关闭"
                    iconName: "x"
                    onClicked: sitesDialog.close()
                }
            }
        }

        Timer {
            id: sitesFilterDelay
            interval: 160
            repeat: false
            onTriggered: appBridge.supportedSiteModel.setFilterText(sitesSearch.text)
        }

        contentItem: ColumnLayout {
            spacing: 6
            Text {
                Layout.fillWidth: true
                text: appBridge.supportedSiteModel.error.length > 0
                    ? "无法读取当前 yt-dlp 提取器列表：" + appBridge.supportedSiteModel.error
                    : "当前 yt-dlp 版本提供 " + appBridge.supportedSiteModel.totalCount + " 个提取器。"
                color: "#a9bed9"
                font.pixelSize: 12
                wrapMode: Text.WordWrap
            }
            UiTextField {
                id: sitesSearch
                Layout.fillWidth: true
                leadingIcon: "search"
                text: appBridge.supportedSiteModel.filterText
                hintText: "搜索 bilibili、tiktok、instagram…"
                onTextEdited: sitesFilterDelay.restart()
                onAccepted: {
                    sitesFilterDelay.stop()
                    appBridge.supportedSiteModel.setFilterText(text)
                }
            }
            UiFlickable {
                id: sitesList
                objectName: "supportedSitesList"
                Layout.fillWidth: true
                Layout.fillHeight: true
                contentWidth: width
                contentHeight: sitesColumn.height
                clip: true

                Column {
                    id: sitesColumn
                    width: sitesList.width
                    height: implicitHeight
                    spacing: 2
                    Repeater {
                        model: appBridge.supportedSiteModel
                        delegate: Rectangle {
                            required property string name
                            required property int index
                            width: sitesColumn.width
                            height: 28
                            radius: 6
                            color: index % 2 ? "#16233a" : "#111d31"
                            Text { anchors.verticalCenter: parent.verticalCenter; anchors.left: parent.left; anchors.leftMargin: 10; text: name; color: "#dcecff"; font.pixelSize: 12 }
                        }
                    }
                }
                ScrollBar.vertical: UiScrollBar { }
            }
            RowLayout {
                Layout.fillWidth: true
                Item { Layout.fillWidth: true }
                UiButton {
                    text: "关闭"
                    iconName: "x"
                    onClicked: sitesDialog.close()
                }
            }
        }
    }

    ScrollView {
        anchors.fill: parent
        clip: true
        contentWidth: availableWidth
        ScrollBar.vertical: UiScrollBar { }
        ScrollBar.horizontal: UiScrollBar { policy: ScrollBar.AlwaysOff }

        ColumnLayout {
            width: parent.width
            spacing: 8

            UiPageTitle {
                Layout.fillWidth: true
                title: "关于"
                subtitle: "应用版本、支持站点和内置组件说明。"
                iconName: "circle-help"
            }

            Rectangle {
                Layout.fillWidth: true
                implicitHeight: identity.implicitHeight + 16
                radius: theme.panelRadius
                color: theme.surface
                border.color: theme.border
                ColumnLayout {
                    id: identity
                    anchors.fill: parent
                    anchors.margins: 8
                    Text { text: appBridge.appName; color: "#eff6ff"; font.pixelSize: 16; font.bold: true }
                    Text { text: "版本 " + appBridge.appVersion + " · Qt Quick"; color: "#a9bed9"; font.pixelSize: 12 }
                    Text {
                        Layout.fillWidth: true
                        text: "本应用整合媒体下载、应用管理的登录与发布浏览器、加密 Cookie 存储以及多平台发布工作流。"
                        color: "#bcd3ef"; wrapMode: Text.WordWrap
                    }
                    Text {
                        Layout.fillWidth: true
                        text: "发布与登录使用应用内置 Playwright Chromium；不调用用户安装的浏览器，也不需要单独安装 Python。"
                        color: "#7f94b2"; font.pixelSize: 12; wrapMode: Text.WordWrap
                    }
                    UiButton {
                        objectName: "supportedSitesButton"
                        text: "查看支持的网站"
                        iconName: "external-link"
                        onClicked: {
                            appBridge.supportedSiteModel.setFilterText("")
                            appBridge.supportedSiteModel.load()
                            sitesDialog.open()
                        }
                    }
                }
            }

            Repeater {
                model: [
                    { name: "social-auto-upload", license: "MIT", icon: "send", role: "内置发布核心，负责登录、Cookie 校验和多平台发布。" },
                    { name: "yt-dlp", license: "Unlicense", icon: "download", role: "提供媒体信息提取和跨站下载。" },
                    { name: "FFmpeg / FFprobe", license: "上游许可证", icon: "settings-2", role: "用于流合并、探测和转码。" },
                    { name: "yt-dlp-ejs / Deno", license: "上游许可证", icon: "file-text", role: "协助 yt-dlp 应对需要 JavaScript 的站点。" },
                    { name: "Playwright / Chromium", license: "Apache-2.0 / Chromium 许可证", icon: "external-link", role: "应用本地的自动化浏览器运行时。" },
                    { name: "PySide6 / Qt", license: "LGPL-3.0 / Qt 商业条款", icon: "sparkles", role: "桌面界面与 Qt Quick 图形层。" }
                ]
                delegate: Rectangle {
                    required property var modelData
                    Layout.fillWidth: true
                    implicitHeight: aboutItem.implicitHeight + 16
                    radius: theme.panelRadius
                    color: theme.surface
                    border.color: theme.border
                    RowLayout {
                        id: aboutItem
                        anchors.fill: parent
                        anchors.margins: 8
                        spacing: 6
                        Rectangle {
                            Layout.preferredWidth: 28
                            Layout.preferredHeight: 28
                            radius: theme.controlRadius
                            color: theme.primarySurface
                            LucideIcon {
                                anchors.centerIn: parent
                                name: modelData.icon
                                color: "#75deec"
                                iconSize: 15
                            }
                        }
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 2
                            Text { Layout.fillWidth: true; text: modelData.name + " · " + modelData.license; color: "#dcecff"; font.pixelSize: 13; font.bold: true; elide: Text.ElideRight }
                            Text { Layout.fillWidth: true; text: modelData.role; color: "#90a5c3"; font.pixelSize: 12; wrapMode: Text.WordWrap }
                        }
                    }
                }
            }
            Item { Layout.preferredHeight: 6 }
        }
    }
}
