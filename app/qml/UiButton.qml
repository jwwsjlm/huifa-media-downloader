import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Button {
    id: root
    UiTheme { id: theme }

    property string tone: highlighted ? "primary" : "secondary"
    property string iconName: ""
    property string accessibleName: ""
    property int iconSize: theme.iconSmall
    property bool iconOnly: text.length === 0
    property color foreground: {
        if (!enabled)
            return theme.textMuted
        if (tone === "primary")
            return theme.canvas
        if (tone === "danger")
            return "#ffdce4"
        if (tone === "ghost" && !checked)
            return hovered ? theme.text : theme.textSecondary
        return theme.text
    }

    implicitHeight: Math.max(
        tone === "primary" ? theme.primaryControlHeight : theme.controlHeight,
        contentItem.implicitHeight + topPadding + bottomPadding
    )
    implicitWidth: Math.max(iconOnly ? implicitHeight : 0, contentItem.implicitWidth + leftPadding + rightPadding)
    // Material reserves 6px above/below its default background. Our flat
    // background must cover the same bounds as the button's hit target.
    topInset: 0
    bottomInset: 0
    leftInset: 0
    rightInset: 0
    leftPadding: iconOnly ? 8 : theme.controlHorizontalPadding
    rightPadding: iconOnly ? 8 : theme.controlHorizontalPadding
    topPadding: 6
    bottomPadding: 6
    hoverEnabled: true
    focusPolicy: Qt.StrongFocus
    Accessible.name: root.accessibleName.length > 0 ? root.accessibleName : root.text
    font.pixelSize: 13
    font.weight: tone === "primary" || checked ? Font.DemiBold : Font.Medium
    contentItem: Item {
        implicitWidth: contentRow.implicitWidth
        implicitHeight: Math.max(contentRow.implicitHeight, root.iconSize)

        RowLayout {
            id: contentRow
            anchors.centerIn: parent
            width: Math.min(implicitWidth, parent.width)
            spacing: root.iconName.length > 0 && root.text.length > 0 ? 7 : 0

            LucideIcon {
                visible: root.iconName.length > 0
                name: root.iconName
                color: root.foreground
                iconSize: root.iconSize
                Layout.alignment: Qt.AlignVCenter
            }
            Text {
                visible: root.text.length > 0
                text: root.text
                color: root.foreground
                font: root.font
                verticalAlignment: Text.AlignVCenter
                elide: Text.ElideRight
                Layout.fillWidth: true
                Layout.minimumWidth: 0
                Layout.alignment: Qt.AlignVCenter
            }
        }
    }

    background: Rectangle {
        radius: theme.controlRadius
        color: {
            if (!root.enabled)
                return theme.disabledSurface
            if (root.tone === "danger")
                return root.down ? "#4b2030" : root.hovered ? "#48243a" : theme.dangerSurface
            if (root.tone === "primary")
                return root.down ? theme.primaryStrong : root.hovered ? "#32d6e8" : theme.primary
            if (root.tone === "ghost")
                return root.checked ? theme.surfaceSelected : root.down ? theme.surfaceRaised : root.hovered ? theme.surfaceHover : "transparent"
            if (root.checked)
                return root.down ? "#173c58" : theme.surfaceSelected
            return root.down ? theme.surfaceRaised : root.hovered ? theme.surfaceHover : theme.surface
        }
        border.width: root.tone === "primary" && !root.visualFocus ? 0 : 1
        border.color: {
            if (!root.enabled)
                return theme.disabledBorder
            if (root.activeFocus)
                return theme.focus
            if (root.tone === "danger")
                return "#844159"
            if (root.tone === "ghost")
                return root.checked ? "#2e8bb2" : "transparent"
            if (root.checked)
                return "#3c8db5"
            return root.hovered ? theme.borderStrong : theme.border
        }

        Behavior on color { ColorAnimation { duration: theme.motionFast } }
        Behavior on border.color { ColorAnimation { duration: theme.motionFast } }
    }
}
