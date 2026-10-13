import QtQuick
import QtQuick.Controls

SpinBox {
    id: root
    UiTheme { id: theme }

    implicitHeight: theme.controlHeight
    font.pixelSize: 13
    focusPolicy: Qt.StrongFocus
    leftPadding: 12
    rightPadding: 34
    topPadding: 0
    bottomPadding: 0
    hoverEnabled: true

    contentItem: TextInput {
        text: root.displayText
        font: root.font
        color: root.enabled ? theme.text : theme.textMuted
        selectionColor: theme.primarySurface
        selectedTextColor: theme.text
        horizontalAlignment: TextInput.AlignLeft
        verticalAlignment: TextInput.AlignVCenter
        readOnly: !root.editable
        validator: root.validator
        inputMethodHints: root.inputMethodHints
        // SpinBox already positions the editor inside its padded content box.
        padding: 0
        clip: true
    }

    up.indicator: Rectangle {
        x: root.width - width - 1
        y: 1
        width: 28
        height: Math.floor((root.height - 3) / 2)
        radius: theme.controlRadius
        color: root.up.pressed ? theme.surfaceSelected : root.up.hovered ? theme.surfaceHover : theme.surfaceRaised
        border.color: theme.border

        LucideIcon {
            anchors.centerIn: parent
            name: "chevron-up"
            iconSize: 12
            color: theme.textSecondary
        }

        Behavior on color { ColorAnimation { duration: theme.motionFast } }
    }

    down.indicator: Rectangle {
        x: root.width - width - 1
        y: root.height - height - 1
        width: 28
        height: Math.floor((root.height - 3) / 2)
        radius: theme.controlRadius
        color: root.down.pressed ? theme.surfaceSelected : root.down.hovered ? theme.surfaceHover : theme.surfaceRaised
        border.color: theme.border

        LucideIcon {
            anchors.centerIn: parent
            name: "chevron-down"
            iconSize: 12
            color: theme.textSecondary
        }

        Behavior on color { ColorAnimation { duration: theme.motionFast } }
    }

    background: Rectangle {
        radius: theme.controlRadius
        color: root.enabled ? theme.field : theme.disabledSurface
        border.width: 1
        border.color: root.activeFocus ? theme.focus : root.hovered ? theme.borderStrong : theme.border

        Behavior on border.color { ColorAnimation { duration: theme.motionFast } }
    }
}
