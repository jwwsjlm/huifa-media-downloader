import QtQuick
import QtQuick.Controls

ProgressBar {
    id: root
    UiTheme { id: theme }

    implicitHeight: 6

    background: Rectangle {
        implicitHeight: 6
        radius: 3
        color: theme.surfaceRaised
    }

    contentItem: Item {
        implicitHeight: 6
        Rectangle {
            width: root.visualPosition * parent.width
            height: parent.height
            radius: height / 2
            color: theme.primary
            Behavior on width { NumberAnimation { duration: theme.motionStandard; easing.type: Easing.OutCubic } }
        }
    }
}
