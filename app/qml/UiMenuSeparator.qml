import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Basic as Basic

Basic.MenuSeparator {
    id: root
    UiTheme { id: theme }

    implicitHeight: 7
    topPadding: 3
    bottomPadding: 3

    contentItem: Rectangle {
        implicitHeight: 1
        color: theme.border
    }
}
