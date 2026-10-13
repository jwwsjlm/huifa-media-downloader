import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: root
    UiTheme { id: theme }
    readonly property bool compactLayout: width < 900

    Timer {
        id: searchDelay
        interval: 180
        repeat: false
        onTriggered: appBridge.publishQueueModel.setSearchText(searchInput.text)
    }

    Component.onCompleted: {
        if (!appBridge.publishQueueModel.loaded || appBridge.publishQueueModel.dirty)
            appBridge.refreshPublishQueue()
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 5

        RowLayout {
            Layout.fillWidth: true
            spacing: 8

            UiPageTitle {
                Layout.fillWidth: true
                title: "发布队列"
                subtitle: appBridge.publishQueueModel.mediaId > 0
                    ? "当前媒体的发布任务"
                    : "所有发布任务一次连续呈现"
                iconName: "send"
            }

            UiButton {
                visible: appBridge.publishQueueModel.mediaId > 0
                text: "显示全部"
                iconName: "list-filter"
                onClicked: appBridge.publishQueueModel.setMediaId(0)
            }
            UiButton {
                text: "刷新"
                iconName: "refresh-cw"
                onClicked: appBridge.refreshPublishQueue()
            }
        }

        Rectangle {
            Layout.fillWidth: true
            radius: theme.panelRadius
            color: theme.surface
            border.color: theme.border
            implicitHeight: controls.implicitHeight + 12

            ColumnLayout {
                id: controls
                anchors.fill: parent
                anchors.margins: 6
                spacing: 4

                UiTextField {
                    id: searchInput
                    Layout.fillWidth: true
                    leadingIcon: "search"
                    text: appBridge.publishQueueModel.searchText
                    hintText: "搜索标题、平台、账号、状态或结果"
                    onTextEdited: searchDelay.restart()
                    onAccepted: {
                        searchDelay.stop()
                        appBridge.publishQueueModel.setSearchText(text)
                    }
                }

                Flow {
                    Layout.fillWidth: true
                    spacing: 4
                    Repeater {
                        model: [
                            { label: "全部", value: "all", icon: "list-filter" },
                            { label: "等待", value: "pending", icon: "list-checks" },
                            { label: "发布中", value: "uploading", icon: "upload" },
                            { label: "成功", value: "success", icon: "check-circle-2" },
                            { label: "失败", value: "failed", icon: "circle-alert" }
                        ]
                        delegate: UiButton {
                            required property var modelData
                            objectName: "publishStatus_" + modelData.value
                            text: modelData.label
                            iconName: modelData.icon
                            iconSize: 14
                            tone: "ghost"
                            checkable: true
                            checked: appBridge.publishQueueModel.statusFilter === modelData.value
                            onClicked: appBridge.publishQueueModel.setStatusFilter(modelData.value)
                        }
                    }
                }
            }
        }

        Rectangle {
            id: queueArea
            Layout.fillWidth: true
            Layout.fillHeight: true
            radius: theme.panelRadius
            color: theme.canvasRaised
            border.color: theme.border
            clip: true

            UiFlickable {
                id: queueList
                objectName: "publishQueueList"
                anchors.fill: parent
                anchors.margins: 5
                contentWidth: width
                contentHeight: queueColumn.height
                clip: true

                Column {
                    id: queueColumn
                    width: queueList.width
                    height: implicitHeight
                    spacing: 4

                    Repeater {
                        model: appBridge.publishQueueModel
                        delegate: Item {
                    id: cardRoot
                    objectName: "publishQueueCard_" + taskId
                    required property int taskId
                    required property string platform
                    required property string account
                    required property string status
                    required property string statusText
                    required property string title
                    required property string result
                    required property string createdAt
                    required property bool canRun
                    required property bool canRetry

                    property bool expanded: false
                    property bool hovered: false
                    property bool pressed: false
                    width: queueColumn.width
                    height: Math.max(70, cardLayout.implicitHeight + 12)
                    clip: true
                    activeFocusOnTab: true
                    Accessible.name: (cardRoot.title || "发布任务") + "，" + cardRoot.statusText
                    Accessible.description: "按回车或空格展开发布操作"

                    Behavior on height {
                        NumberAnimation { duration: theme.motionExpand; easing.type: Easing.OutCubic }
                    }

                    Keys.onReturnPressed: function(event) {
                        cardRoot.expanded = !cardRoot.expanded
                        event.accepted = true
                    }
                    Keys.onSpacePressed: function(event) {
                        cardRoot.expanded = !cardRoot.expanded
                        event.accepted = true
                    }

                    Rectangle {
                        anchors.fill: parent
                        radius: theme.panelRadius
                        color: cardRoot.pressed ? theme.surfaceSelected : cardRoot.hovered ? theme.surfaceHover : theme.surface
                        border.color: cardRoot.activeFocus ? theme.focus : cardRoot.hovered ? theme.borderStrong : theme.border

                        MouseArea {
                            anchors.fill: parent
                            hoverEnabled: true
                            onEntered: cardRoot.hovered = true
                            onExited: cardRoot.hovered = false
                            onPressed: function(mouse) { cardRoot.pressed = mouse.button === Qt.LeftButton }
                            onReleased: cardRoot.pressed = false
                            onCanceled: cardRoot.pressed = false
                            onClicked: function() {
                                cardRoot.forceActiveFocus()
                                cardRoot.expanded = !cardRoot.expanded
                            }
                        }

                        RowLayout {
                            id: cardLayout
                            anchors.top: parent.top
                            anchors.left: parent.left
                            anchors.right: parent.right
                            anchors.margins: 6
                            height: Math.max(58, implicitHeight)
                            spacing: 6

                            Rectangle {
                                Layout.preferredWidth: 52
                                Layout.preferredHeight: 58
                                Layout.alignment: Qt.AlignTop
                                radius: theme.controlRadius
                                color: "#172942"
                                Text {
                                    anchors.centerIn: parent
                                    text: cardRoot.platform || "平台"
                                    color: "#a9d9ff"
                                    font.pixelSize: 11
                                    font.bold: true
                                    elide: Text.ElideRight
                                    width: parent.width - 12
                                    horizontalAlignment: Text.AlignHCenter
                                }
                                LucideIcon {
                                    anchors.horizontalCenter: parent.horizontalCenter
                                    anchors.bottom: parent.bottom
                                    anchors.bottomMargin: 8
                                    name: "send"
                                    color: "#5ecde1"
                                    iconSize: 14
                                }
                            }

                            ColumnLayout {
                                Layout.fillWidth: true
                                Layout.minimumWidth: 0
                                Layout.fillHeight: true
                                spacing: 3

                                RowLayout {
                                    Layout.fillWidth: true
                                    Text {
                                        Layout.fillWidth: true
                                        text: cardRoot.title || "未命名视频"
                                        color: "#eff6ff"
                                    font.pixelSize: 13
                                        font.weight: Font.DemiBold
                                        elide: Text.ElideRight
                                    }
                                    Rectangle {
                                        radius: theme.controlRadius
                                        color: cardRoot.status === "success" ? "#134a3b" : cardRoot.status === "failed" ? "#51252f" : cardRoot.status === "uploading" ? "#163c48" : "#394063"
                                        implicitHeight: 18
                                        implicitWidth: queueStatus.implicitWidth + 10
                                        Text {
                                            id: queueStatus
                                            anchors.centerIn: parent
                                            text: cardRoot.statusText
                                            color: "#d8e7fa"
                                            font.pixelSize: 10
                                        }
                                    }
                                    LucideIcon {
                                        name: "chevron-down"
                                        rotation: cardRoot.expanded ? 180 : 0
                                        transformOrigin: Item.Center
                                        color: cardRoot.activeFocus || cardRoot.hovered ? "#7ce5f1" : "#7890ad"
                                        iconSize: 16

                                        Behavior on rotation { NumberAnimation { duration: theme.motionStandard; easing.type: Easing.OutCubic } }
                                    }
                                }

                                Text {
                                    Layout.fillWidth: true
                                    text: (cardRoot.account || "default") + "  ·  " + (cardRoot.createdAt || "")
                                    color: "#90a5c3"
                                    font.pixelSize: 12
                                    elide: Text.ElideRight
                                }

                                Item {
                                    visible: !cardRoot.expanded
                                    Layout.fillHeight: !cardRoot.expanded
                                }

                                Loader {
                                    Layout.fillWidth: true
                                    Layout.minimumWidth: 0
                                    Layout.preferredHeight: item ? item.implicitHeight : 0
                                    active: cardRoot.expanded
                                    visible: active
                                    sourceComponent: publishActions
                                }
                            }
                        }

                        Component {
                            id: publishActions

                            ColumnLayout {
                                Text {
                                    Layout.fillWidth: true
                                    text: cardRoot.result || "尚未执行"
                                    color: cardRoot.status === "failed" ? "#fca5a5" : "#8496b3"
                                    font.pixelSize: 10
                                    elide: Text.ElideRight
                                }
                                Flow {
                                    Layout.fillWidth: true
                                    Layout.minimumWidth: 0
                                    spacing: 5
                                    UiButton {
                                        visible: cardRoot.canRun
                                        text: cardRoot.canRetry ? "重试发布" : "执行发布"
                                        iconName: cardRoot.canRetry ? "refresh-cw" : "send"
                                        highlighted: true
                                        onClicked: cardRoot.canRetry
                                            ? appBridge.retryPublishTask(cardRoot.taskId)
                                            : appBridge.runPublishTask(cardRoot.taskId)
                                    }
                                }
                            }
                        }
                    }
                        }
                    }
                }

                ScrollBar.vertical: UiScrollBar { }

            }

            UiEmptyState {
                anchors.centerIn: queueArea
                visible: appBridge.publishQueueModel.loaded && appBridge.publishQueueModel.count === 0
                width: Math.min(360, queueArea.width - 40)
                iconName: "send"
                title: "没有匹配的发布任务"
                detail: "从“已完成”页面选择媒体后，可以在这里查看发布进度和重试结果。"
            }
            BusyIndicator {
                anchors.centerIn: queueArea
                running: !appBridge.publishQueueModel.loaded
                visible: running
            }
        }
    }
}
