import QtQuick
import QtQuick.Controls

Slider {
    id: root
    UiTheme { id: theme }

    implicitHeight: 26

    background: Rectangle {
        x: root.leftPadding
        y: root.topPadding + root.availableHeight / 2 - height / 2
        width: root.availableWidth
        height: 6
        radius: 3
        color: theme.surfaceRaised

        Rectangle {
            width: root.visualPosition * parent.width
            height: parent.height
            radius: parent.radius
            color: theme.primary
        }
    }

    handle: Rectangle {
        x: root.leftPadding + root.visualPosition * (root.availableWidth - width)
        y: root.topPadding + root.availableHeight / 2 - height / 2
        width: 17
        height: 17
        radius: width / 2
        color: root.pressed ? theme.focus : root.hovered ? "#68e5f2" : theme.primary
        border.width: 1
        border.color: theme.field
    }
}
