import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Basic as Basic
import QtQuick.Controls.impl

Basic.TextArea {
    id: root
    UiTheme { id: theme }

    property string hintText: ""

    placeholderText: root.hintText
    Accessible.description: root.hintText
    ContextMenu.menu: UiEditContextMenu {
        editor: root
    }

    leftPadding: 12
    rightPadding: 12
    topPadding: 8
    bottomPadding: 8
    selectByMouse: true
    color: theme.text
    placeholderTextColor: theme.textMuted
    selectionColor: theme.primarySurface
    selectedTextColor: theme.text
    font.pixelSize: 13

    background: Rectangle {
        radius: theme.controlRadius
        color: root.enabled ? theme.field : theme.disabledSurface
        border.width: 1
        border.color: root.activeFocus ? theme.focus : root.hovered ? theme.borderStrong : theme.border

        Behavior on border.color { ColorAnimation { duration: theme.motionFast } }
    }

}
