import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Window

UiDialog {
    id: root
    UiTheme { id: theme }
    objectName: "coverStudioDialog"
    parent: Overlay.overlay
    modal: true
    focus: true
    width: Math.min(1020, Overlay.overlay.width - (Overlay.overlay.width < 900 ? 24 : 56))
    height: Math.min(680, Overlay.overlay.height - (root.compactLayout ? 14 : 28))
    anchors.centerIn: parent
    title: appBridge.coverStudio.title || "封面工作室"
    standardButtons: Dialog.NoButton
    closePolicy: Popup.NoAutoClose
    readonly property bool compactLayout: width < 850

    Shortcut {
        sequence: "Escape"
        enabled: root.visible
        onActivated: root.requestClose()
    }

    function present() {
        presetBox.currentIndex = presetBox.indexOfValue(appBridge.coverStudio.preset)
        fitBox.currentIndex = fitBox.indexOfValue(appBridge.coverStudio.fitMode)
        outputPath.text = appBridge.coverStudio.defaultOutputPath
        prompt.text = "保留原始主体和品牌细节，改善构图、光线、清晰度和点击吸引力，不添加误导性文字或人物。"
        open()
    }

    function requestClose() {
        if (appBridge.coverStudio.busy) {
            appBridge.coverStudio.cancelGeneration()
            return
        }
        if (appBridge.coverStudio.dirty) {
            discardDialog.open()
            return
        }
        close()
    }

    onClosed: appBridge.coverStudio.close()

    Connections {
        target: appBridge.coverStudio
        function onChanged() {
            if (!root.visible)
                return
            if (!presetBox.activeFocus)
                presetBox.currentIndex = presetBox.indexOfValue(appBridge.coverStudio.preset)
            if (!fitBox.activeFocus)
                fitBox.currentIndex = fitBox.indexOfValue(appBridge.coverStudio.fitMode)
            if (!outputPath.activeFocus)
                outputPath.text = appBridge.coverStudio.defaultOutputPath
        }
    }

    UiDialog {
        id: discardDialog
        objectName: "coverStudioDiscardDialog"
        parent: root.parent
        modal: true
        width: Math.min(460, root.width - 32)
        title: "放弃未保存的封面修改？"
        anchors.centerIn: parent
        standardButtons: Dialog.Ok | Dialog.Cancel
        contentItem: Text {
            width: 380
            wrapMode: Text.Wrap
            color: "#d8e7fa"
            text: "当前裁切位置或 AI 变体尚未保存。关闭后不会改动原始封面。"
        }
        onAccepted: root.close()
    }

    contentItem: ColumnLayout {
        spacing: 8

        Text {
            Layout.fillWidth: true
            text: "预览和导出均使用本地封面；不会覆盖下载文件。"
            color: "#9eb4d1"
            font.pixelSize: 12
        }

        ScrollView {
            id: coverContentScroll
            objectName: "coverStudioContentScroll"
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.minimumHeight: 0
            Layout.preferredHeight: 0
            clip: true
            contentWidth: availableWidth
            ScrollBar.vertical: UiScrollBar { }
            ScrollBar.horizontal: UiScrollBar { policy: ScrollBar.AlwaysOff }

            GridLayout {
                width: coverContentScroll.availableWidth
                columns: root.compactLayout ? 1 : 2
                columnSpacing: 10
                rowSpacing: 8

                Rectangle {
                    Layout.fillWidth: true
                    Layout.minimumWidth: root.compactLayout ? 0 : 440
                    Layout.minimumHeight: root.compactLayout ? 148 : 330
                    Layout.preferredHeight: root.compactLayout ? 170 : 380
                    Layout.alignment: Qt.AlignTop
                    radius: theme.panelRadius
                    color: theme.field
                    border.color: theme.border
                    clip: true

                Image {
                    id: preview
                    anchors.fill: parent
                    anchors.margins: 10
                    source: appBridge.coverStudio.previewUrl
                    asynchronous: true
                    cache: false
                    sourceSize.width: Math.max(1, Math.ceil(width * Screen.devicePixelRatio))
                    sourceSize.height: Math.max(1, Math.ceil(height * Screen.devicePixelRatio))
                    fillMode: Image.PreserveAspectFit
                    visible: status === Image.Ready
                }

                Text {
                    anchors.centerIn: parent
                    visible: !preview.visible
                    text: appBridge.coverStudio.active ? "正在生成封面预览…" : "没有可用封面"
                    color: "#7187a6"
                }

                MouseArea {
                    anchors.fill: preview
                    enabled: appBridge.coverStudio.fitMode === "crop"
                             && !appBridge.coverStudio.busy
                    cursorShape: enabled ? Qt.CrossCursor : Qt.ArrowCursor
                    onReleased: function(mouse) {
                        if (preview.paintedWidth <= 0 || preview.paintedHeight <= 0)
                            return
                        const paintedX = (width - preview.paintedWidth) / 2
                        const paintedY = (height - preview.paintedHeight) / 2
                        const focusX = (mouse.x - paintedX) / preview.paintedWidth
                        const focusY = (mouse.y - paintedY) / preview.paintedHeight
                        appBridge.coverStudio.setFocusX(
                            Math.max(0, Math.min(100, focusX * 100))
                        )
                        appBridge.coverStudio.setFocusY(
                            Math.max(0, Math.min(100, focusY * 100))
                        )
                    }
                }

                Rectangle {
                    anchors.left: parent.left
                    anchors.bottom: parent.bottom
                    anchors.margins: 10
                    radius: theme.controlRadius
                    color: theme.surfaceRaised
                    implicitHeight: 26
                    implicitWidth: previewHint.implicitWidth + 16
                    Text {
                        id: previewHint
                        anchors.centerIn: parent
                        text: appBridge.coverStudio.fitMode === "crop"
                              ? "单击预览调整裁切焦点"
                              : "留白模式保留完整图像"
                        color: "#bcd3ef"
                        font.pixelSize: 11
                    }
                }
            }

                ColumnLayout {
                    Layout.preferredWidth: root.compactLayout ? 0 : 330
                    Layout.fillWidth: root.compactLayout
                    Layout.alignment: Qt.AlignTop
                    spacing: 8

                    Label { text: "封面预设"; color: "#bcd3ef" }
                    UiComboBox {
                        id: presetBox
                        Layout.fillWidth: true
                        model: appBridge.coverStudio.presets
                        textRole: "label"
                        valueRole: "id"
                        onActivated: appBridge.coverStudio.setPreset(currentValue)
                    }

                    Label { text: "适配方式"; color: "#bcd3ef" }
                    UiComboBox {
                        id: fitBox
                        Layout.fillWidth: true
                        model: [
                            { label: "智能裁切填满", value: "crop" },
                            { label: "完整图像留白", value: "pad" }
                        ]
                        textRole: "label"
                        valueRole: "value"
                        onActivated: appBridge.coverStudio.setFitMode(currentValue)
                    }

                    Label { text: "裁切焦点"; color: "#bcd3ef" }
                    RowLayout {
                        Layout.fillWidth: true
                        enabled: appBridge.coverStudio.fitMode === "crop"
                                 && !appBridge.coverStudio.busy
                        Text { text: "X"; color: "#8298b7" }
                        UiSlider {
                            objectName: "coverFocusXSlider"
                            Layout.fillWidth: true
                            from: 0
                            to: 100
                            value: appBridge.coverStudio.focusX
                            onMoved: appBridge.coverStudio.setFocusX(value)
                        }
                        Label {
                            text: Math.round(appBridge.coverStudio.focusX) + "%"
                            color: "#d8e7fa"
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        enabled: appBridge.coverStudio.fitMode === "crop"
                                 && !appBridge.coverStudio.busy
                        Text { text: "Y"; color: "#8298b7" }
                        UiSlider {
                            Layout.fillWidth: true
                            from: 0
                            to: 100
                            value: appBridge.coverStudio.focusY
                            onMoved: appBridge.coverStudio.setFocusY(value)
                        }
                        Label {
                            text: Math.round(appBridge.coverStudio.focusY) + "%"
                            color: "#d8e7fa"
                        }
                    }

                    Label { text: "JPG 质量"; color: "#bcd3ef" }
                    UiSpinBox {
                        Layout.fillWidth: true
                        from: 50
                        to: 100
                        value: appBridge.coverStudio.quality
                        enabled: !appBridge.coverStudio.busy
                        onValueModified: appBridge.coverStudio.setQuality(value)
                    }

                    UiButton {
                        objectName: "coverSaveOptionsButton"
                        Layout.fillWidth: true
                        text: "保存裁切与质量设置"
                        enabled: appBridge.coverStudio.active && !appBridge.coverStudio.busy
                        onClicked: appBridge.coverStudio.saveOptions()
                    }

                    Rectangle {
                        Layout.fillWidth: true
                        implicitHeight: 1
                        color: "#263755"
                    }

                    Label { text: "保存 JPG"; color: "#bcd3ef" }
                    UiTextField {
                        id: outputPath
                        Layout.fillWidth: true
                        hintText: "JPG 保存路径"
                        selectByMouse: true
                    }
                    UiButton {
                        Layout.fillWidth: true
                        text: "导出 JPG"
                        enabled: appBridge.coverStudio.active && !appBridge.coverStudio.busy
                        onClicked: appBridge.coverStudio.saveJpeg(outputPath.text)
                    }

                    Rectangle {
                        Layout.fillWidth: true
                        implicitHeight: 1
                        color: "#263755"
                    }

                    Label { text: "AI 封面提示词"; color: "#bcd3ef" }
                    UiTextArea {
                        id: prompt
                        Layout.fillWidth: true
                        Layout.preferredHeight: 90
                        wrapMode: TextEdit.Wrap
                        hintText: "描述想要如何改善封面"
                    }
                    UiButton {
                        Layout.fillWidth: true
                        highlighted: true
                        text: appBridge.coverStudio.busy ? "AI 创作中…" : "生成 AI 变体"
                        enabled: appBridge.coverStudio.active && !appBridge.coverStudio.busy
                        onClicked: appBridge.coverStudio.generate(prompt.text)
                    }
                    UiButton {
                        Layout.fillWidth: true
                        visible: appBridge.coverStudio.busy
                        text: "取消 AI 创作"
                        onClicked: appBridge.coverStudio.cancelGeneration()
                    }
                }
            }
        }

        Text {
            Layout.fillWidth: true
            text: appBridge.coverStudio.status
            color: appBridge.coverStudio.status.indexOf("失败") >= 0
                   ? "#fca5a5" : "#9eb4d1"
            wrapMode: Text.Wrap
        }

        RowLayout {
            Layout.fillWidth: true
            UiButton {
                text: "恢复原始封面"
                enabled: appBridge.coverStudio.active && !appBridge.coverStudio.busy
                onClicked: appBridge.coverStudio.restoreOriginal()
            }
            UiButton {
                text: "复制封面"
                enabled: appBridge.coverStudio.active && !appBridge.coverStudio.busy
                onClicked: appBridge.coverStudio.copyCover()
            }
            Item { Layout.fillWidth: true }
            BusyIndicator {
                running: appBridge.coverStudio.busy
                visible: running
                Layout.preferredWidth: 28
                Layout.preferredHeight: 28
            }
            UiButton {
                objectName: "coverStudioCloseButton"
                text: appBridge.coverStudio.busy ? "正在取消…" : "关闭"
                onClicked: root.requestClose()
            }
        }
    }
}
