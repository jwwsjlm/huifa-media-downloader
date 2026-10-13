import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Basic as Basic
import QtQuick.Layouts
import QtQuick.Window

Item {
    id: root
    UiTheme { id: theme }
    objectName: "taskCard_" + taskId

    required property string taskId
    required property string title
    required property string taskKind
    required property string status
    required property string stageText
    required property real progress
    required property string speed
    required property string eta
    required property string size
    required property string error
    required property string selectedQuality
    required property url thumbnailUrl
    required property bool active
    required property bool localHistory

    property bool expanded: false
    property bool hovered: false
    property bool pressed: false
    readonly property bool compact: width < 760
    readonly property int collapsedHeight: compact ? 80 : 72
    readonly property bool canConvert: !localHistory && status === "completed" && taskKind === "video"
    readonly property bool canRedownload: !localHistory && ["completed", "deleted"].includes(status)
    signal deleteRequested(string requestTaskId)
    signal logRequested(string requestTaskId)
    height: Math.max(collapsedHeight, cardLayout.implicitHeight + 12)
    clip: true
    activeFocusOnTab: true
    Accessible.name: (root.title || "下载任务") + "，" + (root.stageText || root.status)
    Accessible.description: "按回车或空格展开操作，右键查看更多操作"

    function openTaskMenu(position) {
        taskMenuLoader.active = true
        const menu = taskMenuLoader.item
        if (!menu)
            return
        menu.rebuild()
        if (position)
            menu.popup(root, position.x, position.y)
        else
            menu.popup(root)
    }

    Behavior on height {
        NumberAnimation { duration: theme.motionExpand; easing.type: Easing.OutCubic }
    }

    Keys.onReturnPressed: function(event) {
        root.expanded = !root.expanded
        event.accepted = true
    }
    Keys.onSpacePressed: function(event) {
        root.expanded = !root.expanded
        event.accepted = true
    }

    Rectangle {
        id: card
        anchors.fill: parent
        radius: theme.panelRadius
        color: root.pressed ? theme.surfaceSelected : root.hovered ? theme.surfaceHover : theme.surface
        border.color: root.activeFocus ? theme.focus : root.hovered ? theme.borderStrong : theme.border
        border.width: 1

        MouseArea {
            anchors.fill: parent
            hoverEnabled: true
            acceptedButtons: Qt.LeftButton | Qt.RightButton
            onEntered: root.hovered = true
            onExited: root.hovered = false
            onPressed: function(mouse) { root.pressed = mouse.button === Qt.LeftButton }
            onReleased: root.pressed = false
            onCanceled: root.pressed = false
            onClicked: function(mouse) {
                root.forceActiveFocus()
                if (mouse.button === Qt.RightButton) {
                    root.openTaskMenu(mapToItem(root, mouse.x, mouse.y))
                    return
                }
                root.expanded = !root.expanded
            }
            onPressAndHold: function(mouse) {
                // Right-click already opens from onClicked. Do not open a
                // second menu from the long-press path on Windows.
                if (mouse.button === Qt.LeftButton)
                    root.openTaskMenu(mapToItem(root, mouse.x, mouse.y))
            }
        }

        RowLayout {
            id: cardLayout
            anchors.top: parent.top
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.margins: 6
            // Measure at the real width, independently of the animated viewport.
            // Wrapped action rows then grow the card instead of overlapping it.
            height: Math.max(root.collapsedHeight - 12, implicitHeight)
            spacing: 6

            Rectangle {
                Layout.preferredWidth: root.compact ? 70 : 76
                Layout.preferredHeight: root.expanded
                    ? (root.compact ? 72 : 62) : root.collapsedHeight - 12
                Layout.alignment: Qt.AlignTop
                radius: theme.controlRadius
                color: "#0b1220"
                clip: true

                Image {
                    id: thumbnail
                    anchors.fill: parent
                    source: root.thumbnailUrl
                    asynchronous: true
                    cache: true
                    sourceSize.width: Math.max(1, Math.ceil(width * Screen.devicePixelRatio))
                    sourceSize.height: Math.max(1, Math.ceil(height * Screen.devicePixelRatio))
                    fillMode: Image.PreserveAspectCrop
                    visible: thumbnail.status === Image.Ready
                }

                ColumnLayout {
                    anchors.centerIn: parent
                    visible: !thumbnail.visible
                    spacing: 4
                    LucideIcon {
                        Layout.alignment: Qt.AlignHCenter
                        name: root.taskKind === "collection" ? "list-checks" : "video"
                        color: "#6a92b7"
                        iconSize: 22
                    }
                    Text {
                        Layout.alignment: Qt.AlignHCenter
                        text: root.taskKind === "collection" ? "合集" : "视频"
                        color: "#6881a5"
                        font.pixelSize: 10
                        font.weight: Font.DemiBold
                    }
                }
            }

            ColumnLayout {
                Layout.fillWidth: true
                Layout.minimumWidth: 0
                Layout.fillHeight: true
                spacing: 3

                RowLayout {
                    id: taskHeading
                    Layout.fillWidth: true
                    spacing: 6

                    Text {
                        Layout.minimumWidth: 0
                        Layout.fillWidth: true
                        text: root.title || "等待获取视频信息"
                        color: "#eff6ff"
                        font.pixelSize: 13
                        font.weight: Font.DemiBold
                        elide: Text.ElideRight
                    }

                    Rectangle {
                        radius: theme.controlRadius
                        color: root.error ? "#51252f" : root.active ? "#163f4c" : "#26334a"
                        implicitHeight: 18
                        implicitWidth: Math.min(
                            statusText.implicitWidth + 12,
                            Math.max(64, taskHeading.width * 0.45)
                        )

                        Text {
                            id: statusText
                            anchors.fill: parent
                            anchors.leftMargin: 6
                            anchors.rightMargin: 6
                            text: root.stageText || root.status
                            color: root.error ? "#fda4af" : root.active ? "#67e8f9" : "#b7c7df"
                            font.pixelSize: 10
                            horizontalAlignment: Text.AlignHCenter
                            verticalAlignment: Text.AlignVCenter
                            elide: Text.ElideRight
                        }
                    }
                    LucideIcon {
                        name: "chevron-down"
                        rotation: root.expanded ? 180 : 0
                        transformOrigin: Item.Center
                        color: root.activeFocus || root.hovered ? "#7ce5f1" : "#7890ad"
                        iconSize: 16

                        Behavior on rotation { NumberAnimation { duration: theme.motionStandard; easing.type: Easing.OutCubic } }
                    }
                }

                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 4
                    radius: 3
                    color: "#202e45"

                    Rectangle {
                        width: parent.width * Math.max(0, Math.min(1, root.progress / 100))
                        height: parent.height
                        radius: parent.radius
                        color: root.active ? "#22d3ee" : "#7287a7"

                        Behavior on width {
                            NumberAnimation { duration: theme.motionStandard; easing.type: Easing.OutCubic }
                        }
                    }
                }

                Flow {
                    Layout.fillWidth: true
                    spacing: 6

                    Text { text: Number(root.progress).toFixed(1) + "%"; color: "#d8e7fa"; font.pixelSize: 10 }
                    Text { text: root.speed || "—"; color: "#82d9eb"; font.pixelSize: 10 }
                    Text { text: root.eta ? "剩余 " + root.eta : ""; color: "#97a9c4"; font.pixelSize: 10 }
                    Text { text: root.size || root.selectedQuality || ""; color: "#97a9c4"; font.pixelSize: 10 }
                }

                Item {
                    visible: !root.expanded
                    Layout.fillHeight: !root.expanded
                }

                Text {
                    Layout.fillWidth: true
                    visible: !root.expanded
                    opacity: root.hovered || root.activeFocus ? 1 : 0
                    text: "单击展开操作 · 右键更多"
                    color: "#7190ae"
                    font.pixelSize: 10
                    elide: Text.ElideRight
                }

                Loader {
                    Layout.fillWidth: true
                    Layout.minimumWidth: 0
                    Layout.preferredHeight: item ? item.implicitHeight : 0
                    active: root.expanded
                    visible: active
                    sourceComponent: expandedActions
                }
            }
        }
    }

    Component {
        id: expandedActions

        ColumnLayout {
            spacing: 3

            Text {
                Layout.fillWidth: true
                text: root.error || root.taskId
                color: root.error ? "#fca5a5" : "#8496b3"
                font.pixelSize: 10
                elide: Text.ElideRight
            }
            Flow {
                Layout.fillWidth: true
                Layout.minimumWidth: 0
                spacing: 5
                UiButton {
                    text: root.status === "paused" ? "继续" : "暂停"
                    iconName: root.status === "paused" ? "play" : "pause"
                    iconSize: 14
                    visible: ["downloading", "queued", "paused"].includes(root.status)
                    onClicked: root.status === "paused"
                               ? appBridge.startTask(root.taskId)
                               : appBridge.pauseTask(root.taskId)
                }
                UiButton {
                    text: "取消"
                    iconName: "x"
                    iconSize: 14
                    tone: "danger"
                    visible: [
                        "downloading", "queued", "processing", "waiting_selection",
                        "parsing_collection", "canceling", "暂停中"
                    ].includes(root.status)
                    onClicked: appBridge.cancelTask(root.taskId)
                }
                UiButton {
                    text: "重试"
                    iconName: "refresh-cw"
                    iconSize: 14
                    visible: ["failed", "canceled", "partial_failed"].includes(root.status)
                    onClicked: appBridge.retryTask(root.taskId)
                }
                UiButton {
                    text: "重新下载"
                    iconName: "download"
                    iconSize: 14
                    visible: root.canRedownload
                    onClicked: appBridge.redownloadTask(root.taskId)
                }
                UiButton {
                    text: "转换格式"
                    iconName: "refresh-cw"
                    iconSize: 14
                    visible: root.canConvert
                    onClicked: appBridge.convertTask(
                        root.taskId,
                        appBridge.settingsModel.values.transcode_encoder || "original"
                    )
                }
                UiButton {
                    text: root.taskKind === "collection" ? "选择项目" : "选择格式"
                    iconName: "list-checks"
                    iconSize: 14
                    visible: root.status === "waiting_selection"
                    onClicked: root.taskKind === "collection"
                               ? appBridge.openCollectionSelection(root.taskId)
                               : appBridge.openFormatSelection(root.taskId)
                }
                UiButton {
                    text: root.taskKind === "collection" ? "查看项目" : "打开"
                    iconName: root.taskKind === "collection" ? "list-checks" : "folder-open"
                    iconSize: 14
                    visible: !(root.taskKind === "collection" && root.status === "waiting_selection")
                    onClicked: root.taskKind === "collection"
                               ? appBridge.openCollectionDetails(root.taskId)
                               : appBridge.openTaskLocation(root.taskId)
                }
                UiButton {
                    text: ""
                    iconName: "ellipsis"
                    accessibleName: "更多操作"
                    iconSize: 17
                    ToolTip.visible: hovered
                    ToolTip.text: "更多操作"
                    onClicked: root.openTaskMenu(mapToItem(root, 0, height))
                }
            }
        }
    }

    Loader {
        id: taskMenuLoader
        active: false
        sourceComponent: taskMenuComponent
    }

    Component {
        id: taskMenuComponent

        Basic.Menu {
            objectName: "taskContextMenu_" + root.taskId
            width: 186
            verticalPadding: 3
            popupType: Popup.Item

            delegate: UiMenuItem { }

            background: Rectangle {
                radius: theme.popupRadius
                color: theme.popup
                border.width: 1
                border.color: theme.borderStrong
            }

            function rebuild() {
                while (count > 0) {
                    const item = itemAt(0)
                    removeItem(item)
                    item.destroy()
                }

                if (root.canConvert)
                    addItem(convertItem.createObject(this))
                if (root.canRedownload)
                    addItem(redownloadItem.createObject(this))
                if (root.canConvert || root.canRedownload)
                    addItem(separatorItem.createObject(this))

                addItem(copySourceItem.createObject(this))
                addItem(copyFolderItem.createObject(this))
                addItem(separatorItem.createObject(this))
                addItem(logItem.createObject(this))
                addItem(separatorItem.createObject(this))
                addItem(deleteItem.createObject(this))
            }

            Component {
                id: convertItem
                UiMenuItem {
                    text: "转换格式"
                    onTriggered: appBridge.convertTask(
                        root.taskId,
                        appBridge.settingsModel.values.transcode_encoder || "original"
                    )
                }
            }
            Component {
                id: redownloadItem
                UiMenuItem {
                    text: "重新下载"
                    onTriggered: appBridge.redownloadTask(root.taskId)
                }
            }
            Component { id: separatorItem; UiMenuSeparator { } }
            Component {
                id: copySourceItem
                UiMenuItem {
                    text: "复制链接 / 本地路径"
                    onTriggered: appBridge.copyTaskSource(root.taskId)
                }
            }
            Component {
                id: copyFolderItem
                UiMenuItem {
                    text: "复制任务文件夹"
                    onTriggered: appBridge.copyTaskFolder(root.taskId)
                }
            }
            Component {
                id: logItem
                UiMenuItem {
                    text: "查看下载日志"
                    onTriggered: root.logRequested(root.taskId)
                }
            }
            Component {
                id: deleteItem
                UiMenuItem {
                    text: "删除任务"
                    onTriggered: root.deleteRequested(root.taskId)
                }
            }
        }
    }
}
