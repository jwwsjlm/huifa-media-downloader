import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Window

Popup {
    id: root
    UiTheme { id: theme }
    objectName: "collectionSelectionPopup"
    modal: true
    focus: true
    anchors.centerIn: Overlay.overlay
    width: Math.min(1160, Overlay.overlay.width - 48)
    height: Math.min(720, Overlay.overlay.height - 40)
    padding: 0
    closePolicy: Popup.NoAutoClose

    enter: Transition {
        ParallelAnimation {
            NumberAnimation { property: "opacity"; from: 0; to: 1; duration: theme.motionFast; easing.type: Easing.OutCubic }
            NumberAnimation { property: "scale"; from: 0.97; to: 1; duration: theme.motionStandard; easing.type: Easing.OutBack; easing.overshoot: 1.05 }
        }
    }
    exit: Transition {
        ParallelAnimation {
            NumberAnimation { property: "opacity"; from: 1; to: 0; duration: theme.motionFast; easing.type: Easing.InCubic }
            NumberAnimation { property: "scale"; from: 1; to: 0.985; duration: theme.motionFast; easing.type: Easing.InCubic }
        }
    }

    property string collectionTitle: "合集"
    property bool viewOnly: false
    readonly property bool compactLayout: width < 820
    signal confirmed()
    signal cancelled()

    function present(taskId, title, readOnly) {
        collectionTitle = title || "合集"
        viewOnly = Boolean(readOnly)
        search.clear()
        appBridge.collectionModel.setFilterText("")
        appBridge.showCollectionEntries(taskId)
        open()
    }

    function dismiss() {
        if (viewOnly) {
            close()
            return
        }
        cancelled()
    }

    Timer {
        id: searchDelay
        interval: 180
        repeat: false
        onTriggered: appBridge.collectionModel.setFilterText(search.text)
    }

    Connections {
        target: appBridge.collectionModel
        function onParentTaskIdChanged() {
            if (!appBridge.collectionModel.parentTaskId)
                root.close()
        }
    }

    background: Rectangle {
        radius: theme.panelRadius
        color: theme.surface
        border.color: theme.borderStrong
    }

    contentItem: ColumnLayout {
        spacing: 0

        RowLayout {
            Layout.fillWidth: true
            Layout.preferredHeight: 54
            Layout.margins: 12
            spacing: 7

            Rectangle {
                Layout.preferredWidth: 28
                Layout.preferredHeight: 28
                radius: theme.controlRadius
                color: "#123c58"
                LucideIcon {
                    anchors.centerIn: parent
                    name: "list-checks"
                    color: "#7de7f2"
                    iconSize: 15
                }
            }
            ColumnLayout {
                Layout.fillWidth: true
                Layout.minimumWidth: 0
                Layout.preferredWidth: Math.max(0, root.width - closeCollectionButton.implicitWidth - 92)
                Text {
                    Layout.fillWidth: true
                    Layout.minimumWidth: 0
                    text: root.collectionTitle
                    color: "#f4f9ff"
                    font.pixelSize: 16
                    font.bold: true
                    elide: Text.ElideRight
                }
                Text {
                    text: "共 " + appBridge.collectionModel.count + " 项 · 已选 " + appBridge.collectionModel.selectedCount + " 项"
                    color: "#92a9c8"
                    font.pixelSize: 12
                }
            }
            UiButton {
                id: closeCollectionButton
                objectName: "collectionCloseButton"
                text: "关闭"
                iconName: "x"
                onClicked: root.dismiss()
            }
        }

        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 1
            color: "#2b405e"
        }

        RowLayout {
            Layout.fillWidth: true
            Layout.margins: 12
            spacing: 6

            UiTextField {
                id: search
                objectName: "collectionSearchInput"
                Layout.fillWidth: true
                leadingIcon: "search"
                hintText: "搜索标题、作者或链接"
                onTextEdited: searchDelay.restart()
                onAccepted: {
                    searchDelay.stop()
                    appBridge.collectionModel.setFilterText(text)
                }
            }
            UiComboBox {
                objectName: "collectionSortMode"
                Layout.preferredWidth: root.compactLayout ? 112 : 145
                model: ["原始顺序", "标题", "作者", "时长", "上传日期"]
                onActivated: appBridge.collectionModel.setSortMode(
                    ["collection_index", "title", "uploader", "duration", "upload_date"][currentIndex],
                    false
                )
            }
            UiButton {
                objectName: "collectionSelectAllButton"
                visible: !root.viewOnly
                text: "全选"
                iconName: "check"
                onClicked: appBridge.collectionModel.setSelectionMode("all")
            }
            UiButton {
                objectName: "collectionSelectNoneButton"
                visible: !root.viewOnly
                text: "全不选"
                iconName: "x"
                onClicked: appBridge.collectionModel.setSelectionMode("none")
            }
        }

        Rectangle {
            id: collectionArea
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.leftMargin: 12
            Layout.rightMargin: 12
            Layout.bottomMargin: 8
            radius: theme.panelRadius
            color: "#0b1322"
            border.color: "#263b59"
            clip: true

            UiFlickable {
                id: collectionList
                objectName: "collectionList"
                anchors.fill: parent
                anchors.margins: 6
                contentWidth: width
                contentHeight: collectionColumn.height
                clip: true

                Column {
                    id: collectionColumn
                    width: collectionList.width
                    height: implicitHeight
                    spacing: 4

                    Repeater {
                        model: appBridge.collectionModel
                        delegate: Rectangle {
                    required property int collectionIndex
                    required property string title
                    required property string uploader
                    required property string durationText
                    required property string uploadDate
                    required property url thumbnailUrl
                    required property string entryKind
                    required property bool downloadable
                    required property string disabledReason
                    required property bool selected
                    required property bool completed

                    width: collectionColumn.width
                    height: 60
                    radius: theme.panelRadius
                    color: selected ? theme.surfaceSelected : theme.surface
                    border.color: selected ? theme.primaryStrong : theme.border

                    Behavior on color { ColorAnimation { duration: theme.motionFast } }
                    Behavior on border.color { ColorAnimation { duration: theme.motionFast } }

                    RowLayout {
                        anchors.fill: parent
                        anchors.margins: 6
                        spacing: 6
                        UiCheckBox {
                            objectName: "collectionEntryCheck_" + collectionIndex
                            checked: selected
                            enabled: downloadable && !root.viewOnly
                            onClicked: appBridge.collectionModel.setSelected(collectionIndex, checked)
                        }
                        Rectangle {
                            Layout.preferredWidth: 72
                            Layout.fillHeight: true
                            radius: theme.controlRadius
                            color: "#15243a"
                            clip: true
                            Image {
                                id: cover
                                anchors.fill: parent
                                source: thumbnailUrl
                                asynchronous: true
                                cache: true
                                sourceSize.width: Math.max(1, Math.ceil(width * Screen.devicePixelRatio))
                                sourceSize.height: Math.max(1, Math.ceil(height * Screen.devicePixelRatio))
                                fillMode: Image.PreserveAspectCrop
                                visible: cover.status === Image.Ready
                            }
                            ColumnLayout {
                                anchors.centerIn: parent
                                visible: !cover.visible
                                spacing: 2
                                LucideIcon {
                                    Layout.alignment: Qt.AlignHCenter
                                    name: entryKind === "collection" ? "list-checks" : "video"
                                    color: "#6a92b7"
                                    iconSize: 18
                                }
                                Text {
                                    Layout.alignment: Qt.AlignHCenter
                                    text: entryKind === "collection" ? "合集" : "视频"
                                    color: "#6980a0"
                                    font.pixelSize: 9
                                }
                            }
                        }
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 2
                            Text {
                                Layout.fillWidth: true
                                text: title || "未命名视频"
                                color: downloadable ? "#eaf3ff" : "#8998af"
                                font.pixelSize: 13
                                font.weight: Font.DemiBold
                                elide: Text.ElideRight
                            }
                            Text {
                                Layout.fillWidth: true
                                text: disabledReason || uploader || entryKind
                                color: disabledReason ? "#f5adad" : "#8499b7"
                                font.pixelSize: 11
                                elide: Text.ElideRight
                            }
                        }
                        Text { text: durationText; color: "#aebfda"; font.pixelSize: 11 }
                        Text {
                            visible: !root.compactLayout
                            text: uploadDate
                            color: "#8397b4"
                            font.pixelSize: 11
                        }
                        Text {
                            text: completed ? "已完成" : entryKind === "collection" ? "合集" : ""
                            color: "#6ed5e1"
                            font.pixelSize: 11
                        }
                    }
                        }
                    }
                }

                ScrollBar.vertical: UiScrollBar { }

            }
            Text {
                anchors.centerIn: collectionArea
                visible: appBridge.collectionModel.count === 0
                text: "没有匹配的项目"
                color: "#7286a4"
            }
        }

        RowLayout {
            Layout.fillWidth: true
            Layout.preferredHeight: 50
            Layout.leftMargin: 12
            Layout.rightMargin: 12
            spacing: 6
            Item { Layout.fillWidth: true }
            UiButton { text: root.viewOnly ? "关闭" : "取消"; iconName: root.viewOnly ? "x" : "chevron-left"; onClicked: root.dismiss() }
            UiButton {
                text: "下载已选项目"
                iconName: "download"
                visible: !root.viewOnly
                highlighted: true
                enabled: appBridge.collectionModel.selectedCount > 0
                onClicked: root.confirmed()
            }
        }
    }
}
