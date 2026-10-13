import QtQuick
import QtQuick.Layouts

Item {
    id: root
    UiTheme { id: theme }

    property string title: ""
    property string subtitle: ""
    property string iconName: "circle-help"
    property color accentColor: "#78e6f2"

    // The inner layout contains a fill-width column, whose implicit width can
    // otherwise claim the whole row and push trailing page actions offscreen.
    implicitWidth: 250
    implicitHeight: 36
    Layout.minimumWidth: 0

    RowLayout {
        id: titleRow
        anchors.fill: parent
        spacing: 6

        Rectangle {
            Layout.preferredWidth: 32
            Layout.preferredHeight: 32
            radius: theme.controlRadius
            color: theme.primarySurface
            border.color: theme.borderStrong
            LucideIcon {
                anchors.centerIn: parent
                name: root.iconName
                color: root.accentColor
                iconSize: 16
            }
        }

        ColumnLayout {
            Layout.fillWidth: true
            spacing: 1
            Text {
                Layout.fillWidth: true
                text: root.title
                color: "#f4f9ff"
                font.pixelSize: 18
                font.bold: true
                elide: Text.ElideRight
            }
            Text {
                Layout.fillWidth: true
                visible: root.subtitle.length > 0
                text: root.subtitle
                color: "#7f9bbb"
                font.pixelSize: 11
                elide: Text.ElideRight
            }
        }
    }
}
