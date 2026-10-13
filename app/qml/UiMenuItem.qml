import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Basic as Basic

Basic.MenuItem {
    id: root
    UiTheme { id: theme }

    implicitHeight: 30
    padding: 0
    leftPadding: 14
    rightPadding: 14
    topPadding: 0
    bottomPadding: 0
    hoverEnabled: true
    font.pixelSize: 13

    contentItem: Text {
        text: root.text
        color: root.enabled ? (root.highlighted ? theme.text : theme.textSecondary) : theme.textMuted
        font: root.font
        verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
    }

    background: Rectangle {
        radius: theme.controlRadius
        color: root.highlighted ? theme.surfaceSelected : "transparent"
    }
}
