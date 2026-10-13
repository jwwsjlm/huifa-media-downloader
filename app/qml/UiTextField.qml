import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Basic as Basic
import QtQuick.Controls.impl

Basic.TextField {
    id: root
    UiTheme { id: theme }

    property string leadingIcon: ""
    property string hintText: ""

    placeholderText: root.hintText
    Accessible.description: root.hintText
    ContextMenu.menu: UiEditContextMenu {
        editor: root
    }

    implicitHeight: theme.controlHeight
    leftPadding: root.leadingIcon.length > 0 ? 36 : 12
    rightPadding: 12
    topPadding: 0
    bottomPadding: 0
    verticalAlignment: TextInput.AlignVCenter
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

    LucideIcon {
        anchors.left: parent.left
        anchors.leftMargin: 12
        anchors.verticalCenter: parent.verticalCenter
        visible: root.leadingIcon.length > 0
        name: root.leadingIcon
        color: root.activeFocus ? theme.focus : theme.textMuted
        iconSize: theme.iconSmall
    }
}
