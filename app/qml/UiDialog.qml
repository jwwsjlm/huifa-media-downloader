import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Dialog {
    id: root
    UiTheme { id: theme }

    padding: 9
    topPadding: padding

    footer: DialogButtonBox {
        visible: count > 0
        spacing: 8
        padding: 9
        topPadding: 9
        bottomPadding: 9
        alignment: Qt.AlignRight
        standardButtons: root.standardButtons
        delegate: UiButton { }
        background: Item { }
    }

    enter: Transition {
        ParallelAnimation {
            NumberAnimation { property: "opacity"; from: 0; to: 1; duration: theme.motionFast; easing.type: Easing.OutCubic }
            NumberAnimation { property: "scale"; from: 0.98; to: 1; duration: theme.motionStandard; easing.type: Easing.OutBack; easing.overshoot: 1.05 }
        }
    }
    exit: Transition {
        ParallelAnimation {
            NumberAnimation { property: "opacity"; from: 1; to: 0; duration: theme.motionFast; easing.type: Easing.InCubic }
            NumberAnimation { property: "scale"; from: 1; to: 0.99; duration: theme.motionFast; easing.type: Easing.InCubic }
        }
    }

    background: Rectangle {
        radius: theme.panelRadius
        color: theme.surface
        border.color: theme.borderStrong
    }

    header: Item {
        implicitHeight: root.title.length > 0 ? 38 : 0
        visible: root.title.length > 0

        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: 12
            anchors.rightMargin: 12
            spacing: 7

            Rectangle {
                Layout.preferredWidth: 4
                Layout.preferredHeight: 18
                radius: 1
                color: theme.primary
            }
            Text {
                Layout.fillWidth: true
                text: root.title
                color: "#eff7ff"
                font.pixelSize: 14
                font.weight: Font.DemiBold
                elide: Text.ElideRight
            }
        }
    }
}
