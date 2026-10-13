import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Basic as Basic

Basic.Menu {
    id: root

    required property var editor
    width: 132
    verticalPadding: 2
    popupType: Popup.Window
    UiTheme { id: theme }

    delegate: UiMenuItem { implicitHeight: 28 }

    background: Rectangle {
        radius: theme.popupRadius
        color: theme.popup
        border.width: 1
        border.color: theme.borderStrong
    }

    Action {
        text: "撤销"
        enabled: !root.editor.readOnly && root.editor.canUndo
        onTriggered: root.editor.undo()
    }
    Action {
        text: "重做"
        enabled: !root.editor.readOnly && root.editor.canRedo
        onTriggered: root.editor.redo()
    }
    UiMenuSeparator { }
    Action {
        text: "剪切"
        enabled: !root.editor.readOnly && root.editor.selectedText.length > 0
        onTriggered: root.editor.cut()
    }
    Action {
        text: "复制"
        enabled: root.editor.selectedText.length > 0
        onTriggered: root.editor.copy()
    }
    Action {
        text: "粘贴"
        enabled: !root.editor.readOnly && root.editor.canPaste
        onTriggered: root.editor.paste()
    }
    Action {
        text: "删除"
        enabled: !root.editor.readOnly && root.editor.selectedText.length > 0
        onTriggered: root.editor.remove(root.editor.selectionStart, root.editor.selectionEnd)
    }
    UiMenuSeparator { }
    Action {
        text: "全选"
        enabled: root.editor.length > 0
        onTriggered: root.editor.selectAll()
    }
}
