import QtQuick
import QtQuick.Controls

ComboBox {
    id: root
    UiTheme { id: theme }

    implicitHeight: theme.controlHeight
    leftPadding: 12
    rightPadding: 34
    topPadding: 0
    bottomPadding: 0
    font.pixelSize: 13
    hoverEnabled: true

    contentItem: Text {
        text: root.displayText
        color: root.enabled ? theme.text : theme.textMuted
        font: root.font
        verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
    }

    indicator: LucideIcon {
        x: root.width - width - 12
        y: (root.height - height) / 2
        name: "chevron-down"
        rotation: root.popup.visible ? 180 : 0
        transformOrigin: Item.Center
        color: root.enabled ? (root.popup.visible ? theme.focus : theme.textSecondary) : theme.textMuted
        iconSize: theme.iconSmall

        Behavior on rotation { NumberAnimation { duration: theme.motionStandard; easing.type: Easing.OutCubic } }
    }

    delegate: ItemDelegate {
        id: option
        objectName: root.objectName.length > 0 ? root.objectName + "_option_" + index : ""
        width: root.popup.width - 8
        height: theme.controlHeight
        leftPadding: 12
        rightPadding: 12
        topPadding: 0
        bottomPadding: 0
        text: root.textAt(index)
        hoverEnabled: true
        highlighted: root.highlightedIndex === index

        contentItem: Text {
            text: option.text
            color: option.highlighted || option.hovered ? theme.text : theme.textSecondary
            font: root.font
            verticalAlignment: Text.AlignVCenter
            elide: Text.ElideRight
        }

        background: Rectangle {
            radius: theme.controlRadius
            color: option.highlighted ? theme.surfaceSelected
                : option.hovered ? theme.surfaceHover : "transparent"
        }
    }

    popup: Popup {
        y: root.height + 2
        width: root.width
        implicitHeight: Math.min(contentItem.implicitHeight + 8, 250)
        padding: 4

        contentItem: ListView {
            clip: true
            implicitHeight: contentHeight
            model: root.delegateModel
            currentIndex: root.highlightedIndex
            spacing: 1
            ScrollBar.vertical: UiScrollBar { }
        }

        background: Rectangle {
            radius: theme.popupRadius
            color: theme.popup
            border.color: theme.borderStrong
            border.width: 1
        }
    }

    background: Rectangle {
        radius: theme.controlRadius
        color: root.enabled ? (root.down || root.popup.visible ? theme.surfaceRaised : theme.field) : theme.disabledSurface
        border.width: 1
        border.color: root.activeFocus || root.popup.visible ? theme.focus : root.hovered ? theme.borderStrong : theme.border

        Behavior on border.color { ColorAnimation { duration: theme.motionFast } }
        Behavior on color { ColorAnimation { duration: theme.motionFast } }
    }
}
