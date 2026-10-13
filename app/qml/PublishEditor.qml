import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

UiDialog {
    id: root
    UiTheme { id: theme }
    objectName: "publishEditorDialog"
    parent: Overlay.overlay
    modal: true
    title: "创建发布任务"
    width: Math.min(1020, parent ? parent.width - 56 : 1020)
    height: Math.min(720, parent ? parent.height - 48 : 720)
    anchors.centerIn: parent
    standardButtons: Dialog.NoButton
    readonly property bool compactLayout: width < 760

    function present() {
        open()
    }

    onClosed: appBridge.publishDraft.close()

    contentItem: ColumnLayout {
        spacing: 8

        Text {
            Layout.fillWidth: true
            text: appBridge.publishDraft.mediaTitle
            color: "#f4f9ff"
            font.pixelSize: 16
            font.weight: Font.DemiBold
            elide: Text.ElideRight
        }

        UiFlickable {
            id: editorScroll
            objectName: "publishEditorScroll"
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.minimumHeight: 0
            clip: true
            contentWidth: width
            contentHeight: editorContent.height
            // The platform list can extend below the fold; keep the scroll affordance
            // visible so the final account rows are discoverable at a glance.
            ScrollBar.vertical: UiScrollBar { policy: ScrollBar.AlwaysOn }
            ScrollBar.horizontal: UiScrollBar { policy: ScrollBar.AlwaysOff }

            ColumnLayout {
                id: editorContent
                width: editorScroll.width
                height: implicitHeight
                spacing: 8

                Rectangle {
                    Layout.fillWidth: true
                    implicitHeight: mediaInfo.implicitHeight + 16
                    radius: theme.panelRadius
                    color: theme.surfaceRaised
                    ColumnLayout {
                        id: mediaInfo
                        anchors.fill: parent
                        anchors.margins: 8
                        Text { text: "来源链接：" + appBridge.publishDraft.sourceUrl; color: "#9bb2d0"; font.pixelSize: 11; elide: Text.ElideMiddle; Layout.fillWidth: true }
                        Text { visible: appBridge.publishDraft.sourceIp.length > 0; text: "来源 IP：" + appBridge.publishDraft.sourceIp; color: "#7f94b2"; font.pixelSize: 11 }
                    }
                }

                GridLayout {
                    Layout.fillWidth: true
                    columns: root.compactLayout ? 1 : 2
                    columnSpacing: 8
                    rowSpacing: 6

                    Label { text: "标题"; color: "#bcd3ef" }
                    UiTextField {
                        Layout.fillWidth: true
                        text: appBridge.publishDraft.title
                        onTextEdited: appBridge.publishDraft.title = text
                    }
                    Label { text: "描述"; color: "#bcd3ef"; Layout.alignment: Qt.AlignTop }
                    UiTextArea {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 82
                        text: appBridge.publishDraft.description
                        wrapMode: TextEdit.Wrap
                        onTextChanged: if (activeFocus) appBridge.publishDraft.description = text
                    }
                    Label { text: "话题"; color: "#bcd3ef" }
                    UiTextField {
                        Layout.fillWidth: true
                        text: appBridge.publishDraft.topics
                        hintText: "#话题1 #话题2"
                        onTextEdited: appBridge.publishDraft.topics = text
                    }
                    Label { text: "来源类型"; color: "#bcd3ef" }
                    UiComboBox {
                        id: sourceType
                        Layout.fillWidth: true
                        model: [{ label: "原创", value: "original" }, { label: "转载", value: "repost" }]
                        textRole: "label"
                        valueRole: "value"
                        currentIndex: appBridge.publishDraft.sourceType === "repost" ? 1 : 0
                        onActivated: appBridge.publishDraft.sourceType = currentValue
                    }
                }

                Rectangle {
                    Layout.fillWidth: true
                    implicitHeight: platformColumn.implicitHeight + 16
                    radius: theme.panelRadius
                    color: theme.surfaceRaised

                    ColumnLayout {
                        id: platformColumn
                        anchors.fill: parent
                        anchors.margins: 8
                        Text { text: "发布平台与账号"; color: "#dcecff"; font.pixelSize: 13; font.bold: true }
                        Repeater {
                            id: platformList
                            model: appBridge.publishDraft.platforms
                            delegate: RowLayout {
                                required property var modelData
                                Layout.fillWidth: true
                                spacing: 6
                            UiCheckBox {
                                objectName: "publishDraftPlatform_" + modelData.platform
                                text: modelData.label
                                checked: modelData.selected
                                Layout.fillWidth: root.compactLayout
                                Layout.preferredWidth: 150
                                    onToggled: appBridge.publishDraft.setPlatformSelected(modelData.platform, checked)
                                }
                                UiTextField {
                                    Layout.fillWidth: true
                                    text: modelData.account
                                    hintText: "账号名称"
                                    onEditingFinished: appBridge.publishDraft.setPlatformAccount(modelData.platform, text)
                                }
                            }
                        }
                    }
                }

                GridLayout {
                    Layout.fillWidth: true
                    columns: root.compactLayout ? 1 : 2
                    columnSpacing: 8
                    rowSpacing: 6

                    Label { visible: appBridge.publishDraft.supportsVisibility; text: "YouTube 可见范围"; color: "#bcd3ef" }
                    UiComboBox {
                        visible: appBridge.publishDraft.supportsVisibility
                        Layout.fillWidth: true
                        model: [{ label: "公开", value: "public" }, { label: "不公开", value: "unlisted" }, { label: "私密", value: "private" }]
                        textRole: "label"
                        valueRole: "value"
                        currentIndex: appBridge.publishDraft.visibility === "unlisted" ? 1 : appBridge.publishDraft.visibility === "private" ? 2 : 0
                        onActivated: appBridge.publishDraft.visibility = currentValue
                    }
                    Label { visible: appBridge.publishDraft.supportsCollection; text: "合集 / 专辑"; color: "#bcd3ef" }
                    UiTextField {
                        visible: appBridge.publishDraft.supportsCollection
                        Layout.fillWidth: true
                        text: appBridge.publishDraft.collection
                        onTextEdited: appBridge.publishDraft.collection = text
                    }
                    Label { visible: appBridge.publishDraft.supportsPlaylist; text: "YouTube 播放列表"; color: "#bcd3ef" }
                    UiTextField {
                        visible: appBridge.publishDraft.supportsPlaylist
                        Layout.fillWidth: true
                        text: appBridge.publishDraft.playlist
                        onTextEdited: appBridge.publishDraft.playlist = text
                    }
                    Label { visible: appBridge.publishDraft.requiresPartition; text: "Bilibili 分区 ID"; color: "#bcd3ef" }
                    UiTextField {
                        visible: appBridge.publishDraft.requiresPartition
                        Layout.fillWidth: true
                        text: appBridge.publishDraft.partition
                        inputMethodHints: Qt.ImhDigitsOnly
                        onTextEdited: appBridge.publishDraft.partition = text
                    }
                    Label { visible: appBridge.publishDraft.supportsSchedule; text: "定时发布"; color: "#bcd3ef" }
                    UiTextField {
                        visible: appBridge.publishDraft.supportsSchedule
                        Layout.fillWidth: true
                        text: appBridge.publishDraft.schedule
                        hintText: "YYYY-MM-DD HH:MM"
                        onTextEdited: appBridge.publishDraft.schedule = text
                    }
                    Label { text: "封面"; color: "#bcd3ef" }
                    UiTextField {
                        Layout.fillWidth: true
                        text: appBridge.publishDraft.thumbnail
                        hintText: "留空则使用下载时保存的封面"
                        onTextEdited: appBridge.publishDraft.thumbnail = text
                    }
                    Label { visible: appBridge.publishDraft.supportsDualThumbnail; text: "横版封面"; color: "#bcd3ef" }
                    UiTextField {
                        visible: appBridge.publishDraft.supportsDualThumbnail
                        Layout.fillWidth: true
                        text: appBridge.publishDraft.thumbnailLandscape
                        onTextEdited: appBridge.publishDraft.thumbnailLandscape = text
                    }
                    Label { visible: appBridge.publishDraft.supportsDualThumbnail; text: "竖版封面"; color: "#bcd3ef" }
                    UiTextField {
                        visible: appBridge.publishDraft.supportsDualThumbnail
                        Layout.fillWidth: true
                        text: appBridge.publishDraft.thumbnailPortrait
                        onTextEdited: appBridge.publishDraft.thumbnailPortrait = text
                    }
                }
            }
        }

        Text {
            Layout.fillWidth: true
            visible: appBridge.publishDraft.lastMessage.length > 0
            text: appBridge.publishDraft.lastMessage
            color: "#f8c577"
            font.pixelSize: 12
            wrapMode: Text.WordWrap
        }

        RowLayout {
            Layout.fillWidth: true
            Item { Layout.fillWidth: true }
            UiButton {
                objectName: "publishEditorCancelButton"
                text: "取消"
                iconName: "chevron-left"
                onClicked: root.close()
            }
            UiButton {
                text: "保存并加入发布队列"
                iconName: "send"
                highlighted: true
                onClicked: appBridge.submitPublishDraft()
            }
        }
    }
}
