import QtQuick
import QtQuick.Layouts

Item {
    id: root
    UiTheme { id: theme }

    property string iconName: "circle-help"
    property string title: ""
    property string detail: ""
    property string actionText: ""
    property string actionIcon: ""
    signal actionRequested()

    implicitWidth: Math.max(280, content.implicitWidth)
    implicitHeight: content.implicitHeight

    ColumnLayout {
        id: content
        anchors.centerIn: parent
        width: Math.min(380, root.width)
        spacing: 7

        Rectangle {
            Layout.alignment: Qt.AlignHCenter
            Layout.preferredWidth: 44
            Layout.preferredHeight: 44
            radius: theme.panelRadius
            color: theme.primarySurface
            LucideIcon {
                anchors.centerIn: parent
                name: root.iconName
                color: "#7ce7f2"
                iconSize: 21
            }
        }
        Text {
            Layout.alignment: Qt.AlignHCenter
            Layout.fillWidth: true
            text: root.title
            color: "#e5f1ff"
            font.pixelSize: 16
            font.weight: Font.DemiBold
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.WordWrap
        }
        Text {
            Layout.alignment: Qt.AlignHCenter
            Layout.fillWidth: true
            visible: root.detail.length > 0
            text: root.detail
            color: "#7f9bbb"
            font.pixelSize: 12
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.WordWrap
        }
        UiButton {
            Layout.alignment: Qt.AlignHCenter
            visible: root.actionText.length > 0
            text: root.actionText
            iconName: root.actionIcon
            highlighted: true
            onClicked: root.actionRequested()
        }
    }
}
