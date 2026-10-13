import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Window

Popup {
    id: root
    UiTheme { id: theme }
    objectName: "formatSelectionPopup"
    modal: true
    focus: true
    anchors.centerIn: Overlay.overlay
    width: Math.min(760, Overlay.overlay.width - 48)
    height: Math.min(620, Overlay.overlay.height - 40)
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

    property int selectedChoiceIndex: -1
    readonly property bool compactLayout: width < 680
    signal confirmed(int choiceIndex)
    signal cancelled()

    function present() {
        selectedChoiceIndex = appBridge.formatModel.firstChoiceIndex
        open()
    }

    background: Rectangle {
        radius: theme.panelRadius
        color: theme.surface
        border.color: theme.borderStrong
    }

    Connections {
        target: appBridge.formatModel
        function onPendingChanged() {
            if (!appBridge.formatModel.pending)
                root.close()
        }
        function onModeChanged() {
            root.selectedChoiceIndex = appBridge.formatModel.firstChoiceIndex
        }
        function onAudioFormatChanged() {
            audioFormat.currentIndex = Math.max(
                0,
                audioFormat.model.indexOf(appBridge.formatModel.audioFormat)
            )
        }
    }

    contentItem: ColumnLayout {
        spacing: 0

        RowLayout {
            Layout.fillWidth: true
            Layout.preferredHeight: 78
            Layout.leftMargin: 12
            Layout.rightMargin: 12
            spacing: 8

            Rectangle {
                Layout.preferredWidth: 100
                Layout.preferredHeight: 58
                radius: theme.controlRadius
                color: "#15243a"
                clip: true
                Image {
                    id: preview
                    anchors.fill: parent
                    source: appBridge.formatModel.thumbnailUrl
                    asynchronous: true
                    sourceSize.width: Math.max(1, Math.ceil(width * Screen.devicePixelRatio))
                    sourceSize.height: Math.max(1, Math.ceil(height * Screen.devicePixelRatio))
                    fillMode: Image.PreserveAspectCrop
                    visible: preview.status === Image.Ready
                }
                ColumnLayout {
                    anchors.centerIn: parent
                    visible: !preview.visible
                    spacing: 4
                    LucideIcon {
                        Layout.alignment: Qt.AlignHCenter
                        name: "video"
                        color: "#6f99be"
                        iconSize: 24
                    }
                    Text {
                        Layout.alignment: Qt.AlignHCenter
                        text: "视频预览"
                        color: "#6b85a8"
                        font.pixelSize: 10
                    }
                }
            }

            ColumnLayout {
                Layout.fillWidth: true
                Text {
                    Layout.fillWidth: true
                    text: appBridge.formatModel.title || "选择下载格式"
                    color: "#f4f9ff"
                    font.pixelSize: 15
                    font.bold: true
                    elide: Text.ElideRight
                }
                Text {
                    text: "选择内容类型与精确格式"
                    color: "#8da4c4"
                    font.pixelSize: 12
                }
            }

            UiButton {
                objectName: "formatCloseButton"
                text: "关闭"
                iconName: "x"
                onClicked: { root.cancelled(); root.close() }
            }
        }

        Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: "#2b405e" }

        RowLayout {
            Layout.fillWidth: true
            Layout.leftMargin: 12
            Layout.rightMargin: 12
            Layout.topMargin: 8
            Layout.bottomMargin: 8
            spacing: 6

            UiButton {
                objectName: "formatMode_video"
                text: "视频"
                iconName: "video"
                checkable: true
                checked: appBridge.formatModel.contentMode === "video"
                enabled: appBridge.formatModel.hasVideo
                onClicked: appBridge.formatModel.setContentMode("video")
            }
            UiButton {
                objectName: "formatMode_audio"
                text: "音频"
                iconName: "library"
                checkable: true
                checked: appBridge.formatModel.contentMode === "audio"
                enabled: appBridge.formatModel.hasAudio
                onClicked: appBridge.formatModel.setContentMode("audio")
            }
            Item { Layout.fillWidth: true }
            Text { text: "音频输出"; color: "#9aafd0"; visible: !root.compactLayout && appBridge.formatModel.contentMode === "audio" }
            UiComboBox {
                id: audioFormat
                visible: appBridge.formatModel.contentMode === "audio"
                Layout.preferredWidth: root.compactLayout ? 112 : 140
                model: ["best", "aac", "alac", "flac", "m4a", "mp3", "opus", "vorbis", "wav"]
                Component.onCompleted: currentIndex = Math.max(0, model.indexOf(appBridge.formatModel.audioFormat))
                onActivated: appBridge.formatModel.setAudioFormat(currentText)
            }
        }

        Rectangle {
            id: choicesArea
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.leftMargin: 12
            Layout.rightMargin: 12
            radius: theme.panelRadius
            color: "#0b1322"
            border.color: "#263b59"
            clip: true

            UiFlickable {
                id: choices
                objectName: "formatChoicesList"
                anchors.fill: parent
                anchors.margins: 6
                contentWidth: width
                contentHeight: choicesColumn.height
                clip: true

                Column {
                    id: choicesColumn
                    width: choices.width
                    height: implicitHeight
                    spacing: 4

                    Repeater {
                        model: appBridge.formatModel
                        delegate: Rectangle {
                    objectName: "formatChoice_" + choiceIndex
                    required property int choiceIndex
                    required property string kind
                    required property string label
                    required property string selector
                    required property string extension
                    required property string codec
                    required property int formatWidth
                    required property int formatHeight
                    required property int sourceHeight
                    required property string fps
                    required property bool hdr
                    required property bool hasAudio
                    required property string language
                    required property string formatNote
                    required property int abr

                    width: choicesColumn.width
                    height: 48
                    radius: theme.panelRadius
                    color: root.selectedChoiceIndex === choiceIndex ? theme.surfaceSelected : theme.surface
                    border.color: root.selectedChoiceIndex === choiceIndex ? theme.primaryStrong : theme.border

                    Behavior on color { ColorAnimation { duration: theme.motionFast } }
                    Behavior on border.color { ColorAnimation { duration: theme.motionFast } }

                    MouseArea {
                        anchors.fill: parent
                        onClicked: root.selectedChoiceIndex = choiceIndex
                    }

                    RowLayout {
                        anchors.fill: parent
                        anchors.margins: 6
                        spacing: 6
                        UiRadioButton {
                            checked: root.selectedChoiceIndex === choiceIndex
                            onClicked: root.selectedChoiceIndex = choiceIndex
                        }
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 2
                            Text {
                                Layout.fillWidth: true
                                text: kind === "audio"
                                      ? (selector === "bestaudio/best" ? "最佳可用音频" : label)
                                      : ((formatHeight || sourceHeight) > 0 ? (formatHeight || sourceHeight) + "p" : label)
                                color: "#eef6ff"
                                font.pixelSize: 13
                                font.weight: Font.DemiBold
                                elide: Text.ElideRight
                            }
                            Text {
                                Layout.fillWidth: true
                                text: kind === "audio"
                                      ? [extension, codec, abr > 0 ? abr + " kbps" : "", language].filter(Boolean).join(" · ")
                                      : [formatWidth > 0 && sourceHeight > 0 ? formatWidth + "×" + sourceHeight : "", extension, fps ? fps + " fps" : "", codec, hdr ? "HDR" : "", hasAudio ? "视频+音频" : "", formatNote].filter(Boolean).join(" · ")
                                color: "#93a9c7"
                                font.pixelSize: 11
                                elide: Text.ElideRight
                            }
                        }
                    }
                        }
                    }
                }

                ScrollBar.vertical: UiScrollBar { }
            }
            Text {
                anchors.centerIn: choicesArea
                visible: appBridge.formatModel.count === 0
                text: "没有可用格式"
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
            UiButton { text: "取消"; iconName: "chevron-left"; onClicked: { root.cancelled(); root.close() } }
            UiButton {
                text: "开始下载"
                iconName: "download"
                highlighted: true
                enabled: root.selectedChoiceIndex >= 0
                onClicked: root.confirmed(root.selectedChoiceIndex)
            }
        }
    }
}
