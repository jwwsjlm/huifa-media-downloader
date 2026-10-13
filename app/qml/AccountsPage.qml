import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: root
    UiTheme { id: theme }
    // This page receives the width after the navigation rail, not the full window.
    // Keep the dense desktop row until that actual content area becomes constrained.
    readonly property bool compactLayout: width < 700

    Component.onCompleted: appBridge.accountModel.reloadSettings()

    ColumnLayout {
        anchors.fill: parent
        spacing: 5

        RowLayout {
            Layout.fillWidth: true
            UiPageTitle {
                Layout.fillWidth: true
                title: "账号"
                subtitle: "设置发布账号、检查 Cookie，并选择默认发布平台。"
                iconName: "user-round"
            }
            UiButton {
                objectName: "accountsSaveButton"
                text: "保存"
                iconName: "check-circle-2"
                highlighted: true
                implicitHeight: theme.primaryControlHeight
                onClicked: appBridge.accountModel.save()
            }
        }

        Rectangle {
            Layout.fillWidth: true
            implicitHeight: accountSummary.implicitHeight + 12
            radius: theme.panelRadius
            color: theme.surface
            border.color: theme.border
            RowLayout {
                id: accountSummary
                anchors.fill: parent
                anchors.margins: 6
                spacing: 6
                Rectangle {
                    Layout.preferredWidth: 26
                    Layout.preferredHeight: 26
                    radius: theme.controlRadius
                    color: "#173e5b"
                    LucideIcon { anchors.centerIn: parent; name: "user-round"; color: "#86e8f5"; iconSize: 15 }
                }
                Text {
                    Layout.fillWidth: true
                    text: root.compactLayout
                        ? "已选 " + appBridge.accountModel.targetCount + " 个平台；登录和检查使用内置 Chromium。"
                        : "已选 " + appBridge.accountModel.targetCount + " 个默认发布平台；登录、检查和发布使用内置 Chromium。"
                    color: "#a9bed9"
                    font.pixelSize: 12
                    wrapMode: Text.WordWrap
                }
            }
        }

        Rectangle {
            Layout.fillWidth: true
            Layout.fillHeight: true
            radius: theme.panelRadius
            color: theme.canvasRaised
            border.color: theme.border
            clip: true

            UiFlickable {
                id: accountsList
                objectName: "accountsList"
                anchors.fill: parent
                anchors.margins: 5
                contentWidth: width
                contentHeight: accountsColumn.height
                clip: true

                Column {
                    id: accountsColumn
                    width: accountsList.width
                    height: implicitHeight
                    spacing: 3

                    Rectangle {
                        visible: !root.compactLayout
                        width: accountsColumn.width
                        height: 20
                        color: "transparent"

                        RowLayout {
                            anchors.fill: parent
                            anchors.leftMargin: 5
                            anchors.rightMargin: 5
                            spacing: 6

                            Text {
                                text: "平台"
                                color: "#6f8dab"
                                font.pixelSize: 11
                                Layout.preferredWidth: 142
                            }
                            Text {
                                text: "账号名"
                                color: "#6f8dab"
                                font.pixelSize: 11
                                Layout.fillWidth: true
                            }
                            Text {
                                text: "状态"
                                color: "#6f8dab"
                                font.pixelSize: 11
                                horizontalAlignment: Text.AlignHCenter
                                Layout.preferredWidth: 68
                            }
                            Text {
                                text: "操作"
                                color: "#6f8dab"
                                font.pixelSize: 11
                                horizontalAlignment: Text.AlignHCenter
                                Layout.preferredWidth: 162
                            }
                        }
                    }

                    Repeater {
                        model: appBridge.accountModel
                        delegate: Rectangle {
                    id: card
                    required property string platform
                    required property string label
                    required property string account
                    required property bool target
                    required property string stateText
                    required property string stateTone
                    required property bool busy
                    required property string loginHint

                    width: accountsColumn.width
                    height: (root.compactLayout ? compactAccountRow.implicitHeight : accountRow.implicitHeight) + 10
                    radius: theme.panelRadius
                    color: theme.surface
                    border.color: theme.border

                    RowLayout {
                        id: accountRow
                        visible: !root.compactLayout
                        anchors.fill: parent
                        anchors.margins: 5
                        spacing: 6

                        UiCheckBox {
                            objectName: "accountTarget_" + card.platform
                            text: card.label
                            checked: card.target
                            onToggled: appBridge.accountModel.setTarget(card.platform, checked)
                            Layout.preferredWidth: 142
                        }
                        UiTextField {
                            id: accountInput
                            Layout.fillWidth: true
                            implicitHeight: theme.controlHeight
                            text: card.account
                            hintText: ""
                            enabled: !card.busy
                            selectByMouse: true
                            onEditingFinished: appBridge.accountModel.setAccount(card.platform, text)
                        }
                        Text {
                            text: card.busy ? "处理中" : card.stateText
                            color: card.busy ? "#f8c577" : card.stateTone === "success" ? "#6ee7a8" : card.stateTone === "danger" ? "#fca5a5" : "#90a5c3"
                            font.pixelSize: 12
                            Layout.preferredWidth: 68
                            horizontalAlignment: Text.AlignHCenter
                        }
                        UiButton {
                            Layout.preferredWidth: 78
                            text: "登录"
                            iconName: "log-in"
                            implicitHeight: theme.controlHeight
                            enabled: !card.busy
                            ToolTip.visible: hovered
                            ToolTip.text: card.loginHint
                            onClicked: {
                                appBridge.accountModel.setAccount(card.platform, accountInput.text)
                                appBridge.accountModel.runAction(card.platform, "login")
                            }
                        }
                        UiButton {
                            Layout.preferredWidth: 78
                            text: "检查"
                            iconName: "check-circle-2"
                            implicitHeight: theme.controlHeight
                            enabled: !card.busy
                            onClicked: {
                                appBridge.accountModel.setAccount(card.platform, accountInput.text)
                                appBridge.accountModel.runAction(card.platform, "check")
                            }
                        }
                    }

                    ColumnLayout {
                        id: compactAccountRow
                        visible: root.compactLayout
                        anchors.fill: parent
                        anchors.margins: 5
                        spacing: 2

                        RowLayout {
                            Layout.fillWidth: true
                            Layout.preferredHeight: theme.controlHeight
                            UiCheckBox {
                                objectName: "accountTargetCompact_" + card.platform
                                Layout.fillWidth: true
                                implicitHeight: theme.controlHeight
                                text: card.label
                                checked: card.target
                                onToggled: appBridge.accountModel.setTarget(card.platform, checked)
                            }
                            Text {
                                text: card.busy ? "处理中" : card.stateText
                                color: card.busy ? "#f8c577" : card.stateTone === "success" ? "#6ee7a8" : card.stateTone === "danger" ? "#fca5a5" : "#90a5c3"
                                font.pixelSize: 12
                            }
                        }
                        UiTextField {
                            id: compactAccountInput
                            Layout.fillWidth: true
                            implicitHeight: theme.controlHeight
                            text: card.account
                            hintText: "账号名称"
                            enabled: !card.busy
                            selectByMouse: true
                            onEditingFinished: appBridge.accountModel.setAccount(card.platform, text)
                        }
                        Flow {
                            Layout.fillWidth: true
                            spacing: 6
                            UiButton {
                                text: "登录"
                                iconName: "log-in"
                                implicitHeight: theme.controlHeight
                                enabled: !card.busy
                                ToolTip.visible: hovered
                                ToolTip.text: card.loginHint
                                onClicked: {
                                    appBridge.accountModel.setAccount(card.platform, compactAccountInput.text)
                                    appBridge.accountModel.runAction(card.platform, "login")
                                }
                            }
                            UiButton {
                                text: "检查"
                                iconName: "check-circle-2"
                                implicitHeight: theme.controlHeight
                                enabled: !card.busy
                                onClicked: {
                                    appBridge.accountModel.setAccount(card.platform, compactAccountInput.text)
                                    appBridge.accountModel.runAction(card.platform, "check")
                                }
                            }
                        }
                    }
                        }
                    }
                }

                ScrollBar.vertical: UiScrollBar { }
            }
        }
    }
}
