import QtQuick
import QtQuick.Controls

CheckBox {
    id: root
    UiTheme { id: theme }

    implicitHeight: theme.controlHeight
    implicitWidth: text.length > 0
        ? implicitContentWidth + leftPadding + rightPadding : implicitHeight
    padding: 6
    topPadding: 4
    bottomPadding: 4
    spacing: 6
    hoverEnabled: true
    font.pixelSize: 13

    indicator: Rectangle {
        implicitWidth: 16
        implicitHeight: 16
        x: root.text.length > 0 ? root.leftPadding
            : root.leftPadding + (root.availableWidth - width) / 2
        y: root.topPadding + (root.availableHeight - height) / 2
        radius: 3
        color: root.checked ? theme.primary : root.hovered ? theme.surfaceHover : theme.field
        border.width: root.visualFocus || !root.checked ? 1 : 0
        border.color: root.activeFocus ? theme.focus : theme.borderStrong

        LucideIcon {
            anchors.centerIn: parent
            visible: root.checked
            name: "check"
            color: theme.canvas
            iconSize: 13
            strokeWidth: 2.8
        }
    }

    contentItem: Text {
        text: root.text
        color: root.enabled ? (root.hovered ? theme.text : theme.textSecondary) : theme.textMuted
        font: root.font
        verticalAlignment: Text.AlignVCenter
        leftPadding: root.text.length > 0 ? root.indicator.width + root.spacing : 0
        elide: Text.ElideRight
    }
}
