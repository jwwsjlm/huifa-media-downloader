import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Basic as Basic

Basic.ScrollBar {
    id: root

    policy: ScrollBar.AsNeeded
    visible: policy === ScrollBar.AlwaysOn
        || (policy === ScrollBar.AsNeeded && size < 1)
    padding: 1
    implicitWidth: 7
    implicitHeight: 7
    minimumSize: 0.06

    contentItem: Rectangle {
        implicitWidth: 5
        implicitHeight: 5
        radius: width / 2
        color: root.pressed ? "#75e6f3" : root.hovered ? "#4f9cc1" : "#315b7f"
        opacity: root.active || root.hovered ? 0.9 : 0.52

        Behavior on color { ColorAnimation { duration: 100 } }
        Behavior on opacity { NumberAnimation { duration: 100 } }
    }

    background: Item {
        implicitWidth: 7
        implicitHeight: 7
    }
}
