import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

UiDialog {
    id: root
    UiTheme { id: theme }
    objectName: "taskLogDialog"
    parent: Overlay.overlay
    modal: true
    width: Math.min(900, Overlay.overlay.width - 40)
    height: Math.min(640, Overlay.overlay.height - 40)
    anchors.centerIn: parent
    title: "下载日志"
    padding: 8
    property string taskId: ""
    property string logText: ""
    standardButtons: Dialog.NoButton

    header: Item {
        implicitHeight: 38

        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: 12
            anchors.rightMargin: 12
            spacing: 6

            Rectangle {
                Layout.preferredWidth: 24
                Layout.preferredHeight: 24
                radius: theme.controlRadius
                color: theme.primarySurface
                LucideIcon {
                    anchors.centerIn: parent
                    name: "file-text"
                    iconSize: 14
                    color: "#7de7f2"
                }
            }
            Text {
                Layout.fillWidth: true
                text: root.title
                color: "#eff7ff"
                font.pixelSize: 14
                font.weight: Font.DemiBold
            }
            UiButton {
                objectName: "taskLogHeaderCloseButton"
                text: ""
                iconName: "x"
                accessibleName: "关闭下载日志"
                iconSize: 14
                ToolTip.visible: hovered
                ToolTip.text: "关闭"
                onClicked: root.close()
            }
        }
    }

    function present(id) {
        taskId = id
        logText = appBridge.taskLogText(id)
        open()
    }

    contentItem: ColumnLayout {
        spacing: 6

        Text {
            Layout.fillWidth: true
            text: "任务 " + root.taskId + " · 日志已脱敏，可用于排查下载、登录、网络和格式问题。"
            color: "#9eb4d1"
            font.pixelSize: 12
            elide: Text.ElideMiddle
        }

        ScrollView {
            id: logScroll
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.minimumWidth: 0
            Layout.minimumHeight: 0
            clip: true
            contentWidth: availableWidth
            ScrollBar.vertical: UiScrollBar { }
            ScrollBar.horizontal: UiScrollBar { policy: ScrollBar.AlwaysOff }

            UiTextArea {
                id: content
                width: logScroll.availableWidth
                readOnly: true
                selectByMouse: true
                wrapMode: TextEdit.Wrap
                text: root.logText
                color: "#dcecff"
                font.family: "Consolas"
                font.pixelSize: 12
            }
        }

        RowLayout {
            Layout.fillWidth: true
            UiButton {
                text: "刷新"
                onClicked: root.logText = appBridge.taskLogText(root.taskId)
            }
            UiButton {
                text: "复制日志"
                onClicked: appBridge.copyText(content.text)
            }
            UiButton {
                text: "清除日志"
                onClicked: {
                    appBridge.clearTaskLog(root.taskId)
                    root.logText = appBridge.taskLogText(root.taskId)
                }
            }
            UiButton {
                text: "打开日志目录"
                onClicked: appBridge.openTaskLogLocation(root.taskId)
            }
            Item { Layout.fillWidth: true }
            UiButton {
                objectName: "taskLogCloseButton"
                text: "关闭"
                iconName: "x"
                onClicked: root.close()
            }
        }
    }
}
