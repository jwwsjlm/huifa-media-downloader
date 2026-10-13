import QtQuick
import QtQuick.Controls

RadioButton {
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
        implicitWidth: 18
        implicitHeight: 18
        x: root.text.length > 0 ? root.leftPadding
            : root.leftPadding + (root.availableWidth - width) / 2
        y: root.topPadding + (root.availableHeight - height) / 2
        radius: width / 2
        color: root.checked ? theme.primarySurface : root.hovered ? theme.surfaceHover : theme.field
        border.width: root.checked ? 2 : 1
        border.color: root.checked ? theme.primary : root.activeFocus ? theme.focus : theme.borderStrong

        Rectangle {
            anchors.centerIn: parent
            width: 8
            height: 8
            radius: width / 2
            visible: root.checked
            color: theme.primary
        }
    }

    contentItem: Text {
        text: root.text
        color: root.enabled ? theme.textSecondary : theme.textMuted
        font: root.font
        leftPadding: root.text.length > 0 ? root.indicator.width + root.spacing : 0
        verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
    }
}
