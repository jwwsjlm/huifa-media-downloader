import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Window

Item {
    id: root
    UiTheme { id: theme }
    readonly property bool compactLayout: width < 900

    Timer {
        id: searchDelay
        interval: 180
        repeat: false
        onTriggered: appBridge.completedMediaModel.setSearchText(searchInput.text)
    }

    Component.onCompleted: appBridge.refreshCompletedMedia()

    ColumnLayout {
        anchors.fill: parent
        spacing: 5

        RowLayout {
            Layout.fillWidth: true
            spacing: 8

            UiPageTitle {
                Layout.fillWidth: true
                title: "已完成"
                subtitle: appBridge.completedMediaModel.loaded
                    ? "媒体库 " + appBridge.completedMediaModel.count + " / " + appBridge.completedMediaModel.totalCount
                    : "打开时加载完整媒体库"
                iconName: "library"
            }

            UiButton {
                text: "刷新"
                iconName: "refresh-cw"
                onClicked: appBridge.refreshCompletedMedia()
            }
        }

        Rectangle {
            Layout.fillWidth: true
            radius: theme.panelRadius
            color: theme.surface
            border.color: theme.border
            implicitHeight: filters.implicitHeight + 12

            ColumnLayout {
                id: filters
                anchors.fill: parent
                anchors.margins: 6
                spacing: 4

                RowLayout {
                    Layout.fillWidth: true
                    UiTextField {
                        id: searchInput
                        Layout.fillWidth: true
                        leadingIcon: "search"
                        text: appBridge.completedMediaModel.searchText
                        hintText: "搜索标题、作者、路径或平台"
                        onTextEdited: searchDelay.restart()
                        onAccepted: {
                            searchDelay.stop()
                            appBridge.completedMediaModel.setSearchText(text)
                        }
                    }
                    Text {
                        visible: !root.compactLayout
                        text: "按发布状态筛选"
                        color: "#7793b1"
                        font.pixelSize: 11
                    }
                }

                Flow {
                    Layout.fillWidth: true
                    spacing: 4
                    Repeater {
                        model: [
                            { label: "全部", value: "all", count: appBridge.completedMediaModel.totalCount, icon: "library" },
                            { label: "待发布", value: "needs_distribution", count: appBridge.completedMediaModel.needsDistributionCount, icon: "send" },
                            { label: "已发布", value: "published", count: appBridge.completedMediaModel.publishedCount, icon: "check-circle-2" },
                            { label: "队列中", value: "queued", count: appBridge.completedMediaModel.queuedCount, icon: "list-checks" },
                            { label: "需重试", value: "retry_needed", count: appBridge.completedMediaModel.retryNeededCount, icon: "refresh-cw" },
                            { label: "全部完成", value: "complete", count: appBridge.completedMediaModel.completeCount, icon: "check" }
                        ]
                        delegate: UiButton {
                            required property var modelData
                            objectName: "completedFilter_" + modelData.value
                            text: modelData.label + " " + modelData.count
                            iconName: modelData.icon
                            iconSize: 14
                            tone: "ghost"
                            checkable: true
                            checked: appBridge.completedMediaModel.filter === modelData.value
                            onClicked: appBridge.completedMediaModel.setFilter(modelData.value)
                        }
                    }
                }
            }
        }

        Rectangle {
            id: mediaArea
            Layout.fillWidth: true
            Layout.fillHeight: true
            radius: theme.panelRadius
            color: theme.canvasRaised
            border.color: theme.border
            clip: true

            UiFlickable {
                id: mediaList
                objectName: "completedMediaList"
                anchors.fill: parent
                anchors.margins: 5
                contentWidth: width
                contentHeight: mediaColumn.height
                clip: true

                Column {
                    id: mediaColumn
                    width: mediaList.width
                    height: implicitHeight
                    spacing: 4

                    Repeater {
                        model: appBridge.completedMediaModel
                        delegate: Item {
                    id: cardRoot
                    objectName: "completedCard_" + mediaId
                    required property int mediaId
                    required property string title
                    required property string uploader
                    required property url thumbnailUrl
                    required property string videoPath
                    required property string sourcePlatform
                    required property string downloadedAt
                    required property string distributionText
                    required property bool published
                    required property bool queued
                    required property bool retryNeeded
                    required property bool complete

                    property bool expanded: false
                    property bool hovered: false
                    property bool pressed: false
                    readonly property bool compact: width < 760
                    width: mediaColumn.width
                    readonly property int collapsedHeight: compact ? 90 : 76
                    height: Math.max(collapsedHeight, cardLayout.implicitHeight + 12)
                    clip: true
                    activeFocusOnTab: true
                    Accessible.name: (cardRoot.title || "已完成媒体") + "，" + (cardRoot.distributionText || "待发布")
                    Accessible.description: "按回车或空格展开媒体操作"

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
                            height: Math.max(cardRoot.collapsedHeight - 12, implicitHeight)
                            spacing: 6

                            Rectangle {
                                Layout.preferredWidth: cardRoot.compact ? 72 : 88
                                Layout.preferredHeight: cardRoot.expanded
                                    ? (cardRoot.compact ? 72 : 62) : cardRoot.collapsedHeight - 12
                                Layout.alignment: Qt.AlignTop
                                radius: theme.controlRadius
                                color: "#0b1220"
                                clip: true

                                Image {
                                    id: cover
                                    anchors.fill: parent
                                    source: cardRoot.thumbnailUrl
                                    asynchronous: true
                                    cache: true
                                    sourceSize.width: Math.max(1, Math.ceil(width * Screen.devicePixelRatio))
                                    sourceSize.height: Math.max(1, Math.ceil(height * Screen.devicePixelRatio))
                                    fillMode: Image.PreserveAspectCrop
                                    visible: status === Image.Ready
                                }
                                ColumnLayout {
                                    anchors.centerIn: parent
                                    visible: !cover.visible
                                    spacing: 4
                                    LucideIcon {
                                        Layout.alignment: Qt.AlignHCenter
                                        name: "library"
                                        color: "#6a92b7"
                                        iconSize: 23
                                    }
                                    Text {
                                        Layout.alignment: Qt.AlignHCenter
                                        text: "媒体"
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
                                        color: cardRoot.complete ? "#134a3b" : cardRoot.retryNeeded ? "#51252f" : cardRoot.queued ? "#394063" : "#163c48"
                                        implicitHeight: 18
                                        implicitWidth: distributionState.implicitWidth + 10
                                        Text {
                                            id: distributionState
                                            anchors.centerIn: parent
                                            text: cardRoot.complete ? "发布完成" : cardRoot.retryNeeded ? "需要重试" : cardRoot.queued ? "发布队列中" : cardRoot.published ? "部分已发布" : "待发布"
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
                                    text: (cardRoot.uploader || "未知作者") + "  ·  " + (cardRoot.downloadedAt || "下载时间未知")
                                    color: "#90a5c3"
                                    font.pixelSize: 12
                                    elide: Text.ElideRight
                                }

                                Text {
                                    Layout.fillWidth: true
                                    text: cardRoot.distributionText || "尚未创建发布任务"
                                    color: "#82d9eb"
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
                                    sourceComponent: completedActions
                                }
                            }
                        }

                        Component {
                            id: completedActions

                            ColumnLayout {
                                Text {
                                    Layout.fillWidth: true
                                    text: cardRoot.videoPath || "未记录视频路径"
                                    color: "#8496b3"
                                    font.pixelSize: 10
                                    elide: Text.ElideMiddle
                                }
                                Flow {
                                    Layout.fillWidth: true
                                    Layout.minimumWidth: 0
                                    spacing: 5
                                    UiButton {
                                        text: "打开文件夹"
                                        iconName: "folder-open"
                                        iconSize: 14
                                        onClicked: appBridge.openMediaLocation(cardRoot.mediaId)
                                    }
                                    UiButton {
                                        text: "发布队列"
                                        iconName: "list-checks"
                                        iconSize: 14
                                        onClicked: appBridge.showPublishQueueForMedia(cardRoot.mediaId)
                                    }
                                    UiButton {
                                        text: "封面工作室"
                                        iconName: "image"
                                        iconSize: 14
                                        onClicked: appBridge.openCoverStudio(cardRoot.mediaId)
                                    }
                                    UiButton {
                                        text: "发布"
                                        iconName: "send"
                                        iconSize: 14
                                        highlighted: true
                                        onClicked: appBridge.openPublishEditor(cardRoot.mediaId)
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
                anchors.centerIn: mediaArea
                visible: appBridge.completedMediaModel.loaded && appBridge.completedMediaModel.count === 0
                width: Math.min(360, mediaArea.width - 40)
                iconName: "library"
                title: "没有匹配的已完成媒体"
                detail: "完成下载后的媒体会显示在这里，可继续生成封面和创建发布任务。"
            }
            BusyIndicator {
                anchors.centerIn: mediaArea
                running: !appBridge.completedMediaModel.loaded
                visible: running
            }
        }
    }
}
