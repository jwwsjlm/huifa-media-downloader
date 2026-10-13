import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Window
import QtQuick.Controls.Material

ApplicationWindow {
    id: window
    objectName: "mainWindow"
    visible: true
    UiTheme { id: theme }
    width: theme.defaultWindowWidth
    height: theme.defaultWindowHeight
    minimumWidth: theme.minimumWindowWidth
    minimumHeight: theme.minimumWindowHeight
    title: "HuifaMediaDownloader"
    color: "#0a1020"
    Material.theme: Material.Dark
    Material.primary: "#1d4ed8"
    Material.accent: "#38bdf8"
    Material.background: "#101a2d"
    Material.foreground: "#edf5ff"
    palette {
        window: "#0a1020"
        windowText: "#eff6ff"
        base: "#0d1728"
        alternateBase: "#132038"
        text: "#eff6ff"
        placeholderText: "#7f94b2"
        button: "#17253a"
        buttonText: "#dbeafe"
        highlight: "#1e88c8"
        highlightedText: "#f8fbff"
        toolTipBase: "#17253a"
        toolTipText: "#eff6ff"
    }
    property int currentPage: 0
    property string activeQuickAction: ""
    readonly property bool compactSidebar: width < theme.wideBreakpoint
    readonly property bool narrowLayout: taskPage.width < theme.taskWideContentBreakpoint
    readonly property bool singleColumnLayout: taskPage.width < theme.taskSingleColumnContentBreakpoint
    readonly property var navigationItems: [
        { label: "下载任务", icon: "download" },
        { label: "账号", icon: "user-round" },
        { label: "已完成", icon: "library" },
        { label: "发布队列", icon: "send" },
        { label: "设置", icon: "settings-2" },
        { label: "关于", icon: "circle-help" }
    ]

    onClosing: function(close) {
        if (appBridge.shutdownReady) {
            close.accepted = true
            return
        }
        close.accepted = false
        if (!appBridge.shutdownStarted)
            appBridge.requestShutdown()
    }

    Timer {
        id: filterDelay
        interval: 180
        repeat: false
        onTriggered: appBridge.taskModel.setFilterText(filterInput.text)
    }

    CollectionSelection {
        id: collectionSelection
        onConfirmed: {
            if (appBridge.confirmCollectionSelection(appBridge.collectionModel.parentTaskId))
                close()
        }
        onCancelled: {
            if (appBridge.cancelCollectionSelection(appBridge.collectionModel.parentTaskId))
                close()
        }
    }

    FormatSelection {
        id: formatSelection
        onConfirmed: function(choiceIndex) {
            appBridge.confirmFormatSelection(choiceIndex)
            close()
        }
        onCancelled: appBridge.cancelFormatSelection()
    }

    PublishEditor { id: publishEditor }

    CoverStudio {
        id: coverStudio
    }

    TaskLog {
        id: taskLog
    }

    UiDialog {
        id: deleteTaskDialog
        objectName: "deleteTaskDialog"
        parent: Overlay.overlay
        modal: true
        width: Math.min(480, Overlay.overlay.width - 48)
        anchors.centerIn: parent
        title: "删除任务"
        property string taskId: ""
        standardButtons: Dialog.NoButton

        contentItem: ColumnLayout {
            spacing: 14

            Text {
                Layout.fillWidth: true
                wrapMode: Text.Wrap
                color: "#d8e7fa"
                text: "删除此任务记录？正在下载或转换的任务会先停止。"
            }
            Text {
                Layout.fillWidth: true
                wrapMode: Text.Wrap
                color: "#8fa4c1"
                font.pixelSize: 12
                text: "仅删除记录会保留媒体文件和“已完成”目录。删除媒体只会清理该任务管理的文件。"
            }
            RowLayout {
                Layout.fillWidth: true
                spacing: 8
                Item { Layout.fillWidth: true }
                UiButton { objectName: "deleteTaskCancelButton"; text: "取消"; onClicked: deleteTaskDialog.close() }
                UiButton {
                    text: "仅删除记录"
                    onClicked: {
                        if (appBridge.deleteTask(deleteTaskDialog.taskId, false))
                            deleteTaskDialog.close()
                    }
                }
                UiButton {
                    text: "删除记录和媒体"
                    highlighted: true
                    onClicked: {
                        if (appBridge.deleteTask(deleteTaskDialog.taskId, true))
                            deleteTaskDialog.close()
                    }
                }
            }
        }
    }

    UiDialog {
        id: cleanupCompletedDialog
        objectName: "cleanupCompletedDialog"
        parent: Overlay.overlay
        modal: true
        width: Math.min(460, Overlay.overlay.width - 48)
        anchors.centerIn: parent
        title: "清理已完成任务"
        standardButtons: Dialog.NoButton

        contentItem: ColumnLayout {
            spacing: 14

            Text {
                Layout.fillWidth: true
                wrapMode: Text.Wrap
                color: "#d8e7fa"
                text: "清理所有已完成的任务记录？媒体文件和“已完成”目录不会删除。"
            }
            RowLayout {
                Layout.fillWidth: true
                Item { Layout.fillWidth: true }
                UiButton { objectName: "cleanupCompletedCancelButton"; text: "取消"; onClicked: cleanupCompletedDialog.close() }
                UiButton {
                    text: "清理记录"
                    highlighted: true
                    onClicked: {
                        appBridge.cleanupCompletedTasks()
                        cleanupCompletedDialog.close()
                    }
                }
            }
        }
    }

    UiDialog {
        id: localHistoryDialog
        objectName: "localHistoryDialog"
        parent: Overlay.overlay
        modal: true
        width: Math.min(520, Overlay.overlay.width - 48)
        height: 220
        anchors.centerIn: parent
        property string folder: ""
        property int candidateCount: 0
        title: "导入已有媒体"
        standardButtons: Dialog.NoButton
        contentItem: ColumnLayout {
            spacing: 10
            Text {
                Layout.fillWidth: true
                wrapMode: Text.Wrap
                color: "#d8e7fa"
                text: "在 " + localHistoryDialog.folder + " 中发现 "
                      + localHistoryDialog.candidateCount
                      + " 个未记录媒体。导入后会显示为已完成任务，原始文件不会被移动或修改。"
            }
            RowLayout {
                Layout.fillWidth: true
                Item { Layout.fillWidth: true }
                UiButton {
                    objectName: "localHistoryCancelButton"
                    text: "取消"
                    onClicked: localHistoryDialog.close()
                }
                UiButton {
                    objectName: "localHistoryImportButton"
                    text: "导入媒体"
                    iconName: "library"
                    highlighted: true
                    onClicked: {
                        appBridge.importScannedLocalHistory()
                        localHistoryDialog.close()
                    }
                }
            }
        }
    }

    UiDialog {
        id: databaseRecoveryDialog
        objectName: "databaseRecoveryDialog"
        parent: Overlay.overlay
        modal: true
        width: Math.min(520, Overlay.overlay.width - 48)
        anchors.centerIn: parent
        property string heading: ""
        property string summary: ""
        property string detail: ""
        title: heading
        standardButtons: Dialog.Ok
        contentItem: Text {
            width: 480
            wrapMode: Text.Wrap
            color: "#d8e7fa"
            text: databaseRecoveryDialog.summary + "\n\n" + databaseRecoveryDialog.detail
        }
    }

    Connections {
        target: appBridge
        function onCollectionSelectionRequested(taskId, title) {
            collectionSelection.present(taskId, title, false)
        }
        function onCollectionDetailsRequested(taskId, title) {
            collectionSelection.present(taskId, title, true)
        }
        function onFormatSelectionRequested() {
            formatSelection.present()
        }
        function onNavigationRequested(index) {
            window.currentPage = index
        }
        function onWindowActivationRequested() {
            if (window.visibility === Window.Minimized)
                window.showNormal()
            else
                window.show()
            window.raise()
            window.requestActivate()
        }
        function onTaskFocusRequested(taskId) {
            window.currentPage = 0
            filterDelay.stop()
            filterInput.text = taskId
            appBridge.taskModel.setStatusFilter("all")
            appBridge.taskModel.setFilterText(taskId)
        }
        function onPublishTaskFocusRequested(taskId) {
            window.currentPage = 3
            appBridge.publishQueueModel.setMediaId(0)
            appBridge.publishQueueModel.setSearchText(String(taskId))
        }
        function onPublishEditorRequested() {
            publishEditor.present()
        }
        function onPublishEditorCompleted() {
            publishEditor.close()
        }
        function onCoverStudioRequested() {
            coverStudio.present()
        }
        function onShutdownCompleted() {
            window.close()
        }
        function onLocalHistoryImportRequested(folder, count) {
            localHistoryDialog.folder = folder
            localHistoryDialog.candidateCount = count
            localHistoryDialog.open()
        }
        function onDatabaseRecoveryRequested(title, summary, detail) {
            databaseRecoveryDialog.heading = title
            databaseRecoveryDialog.summary = summary
            databaseRecoveryDialog.detail = detail
            databaseRecoveryDialog.open()
        }
    }

    Rectangle {
        anchors.fill: parent
        color: theme.canvas
    }

    RowLayout {
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: statusBar.top
        anchors.topMargin: theme.windowMargin
        anchors.leftMargin: theme.windowMargin
        anchors.rightMargin: theme.windowMargin
        anchors.bottomMargin: theme.windowMargin
        spacing: theme.windowGap

        Rectangle {
            id: navigationRail
            Layout.alignment: Qt.AlignTop
            Layout.preferredWidth: window.compactSidebar
                ? theme.compactSidebarWidth : theme.wideSidebarWidth
            Layout.preferredHeight: navigationRailContent.implicitHeight
                + 10
            radius: theme.panelRadius
            clip: true
            color: "#0d1728"
            border.color: "#203b58"

            ColumnLayout {
                id: navigationRailContent
                anchors.fill: parent
                anchors.margins: 5
                spacing: 0

                RowLayout {
                    Layout.fillWidth: true
                    Layout.alignment: window.compactSidebar ? Qt.AlignHCenter : Qt.AlignLeft
                    spacing: 7

                    Rectangle {
                        Layout.preferredWidth: 30
                        Layout.preferredHeight: 30
                        radius: theme.controlRadius
                        color: "#174a69"
                        LucideIcon {
                            anchors.centerIn: parent
                            name: "download"
                            color: "#8cecf7"
                            iconSize: 18
                        }
                    }
                    ColumnLayout {
                        visible: !window.compactSidebar
                        Layout.fillWidth: true
                        Layout.minimumWidth: 0
                        spacing: 1
                        Text {
                            Layout.fillWidth: true
                            text: "Huifa"
                            color: "#f4f9ff"
                            font.pixelSize: 19
                            font.bold: true
                            elide: Text.ElideRight
                        }
                        Text {
                            Layout.fillWidth: true
                            text: "MEDIA DOWNLOADER"
                            color: "#6f8eb4"
                            font.pixelSize: 10
                            font.letterSpacing: 1.2
                            elide: Text.ElideRight
                        }
                    }
                }

                Item { Layout.preferredHeight: 2 }

                Repeater {
                    model: window.navigationItems
                    delegate: UiButton {
                        id: navigationButton
                        required property var modelData
                        required property int index
                        objectName: "navigationButton_" + index
                        Layout.fillWidth: true
                        // Keep a comfortable hit target around the larger icons.
                        // Every slot has the same size, so page switches never reflow it.
                        implicitHeight: 36
                        leftPadding: 8
                        rightPadding: 8
                        checkable: true
                        checked: navigationButton.index === window.currentPage
                        tone: "ghost"
                        hoverEnabled: true
                        accessibleName: modelData.label
                        onClicked: window.currentPage = index

                        ToolTip {
                            parent: navigationButton
                            visible: window.compactSidebar && navigationButton.hovered
                            text: navigationButton.modelData.label
                            delay: 300
                            x: navigationButton.width + 6
                            y: (navigationButton.height - height) / 2
                            exit: Transition { NumberAnimation { property: "opacity"; duration: 0 } }
                        }

                        contentItem: Item {
                            implicitWidth: window.compactSidebar ? 24 : navigationContent.implicitWidth
                            implicitHeight: 24
                            RowLayout {
                                id: navigationContent
                                anchors.fill: parent
                                spacing: 7
                                Item {
                                    Layout.preferredWidth: 24
                                    Layout.preferredHeight: 24

                                    LucideIcon {
                                        anchors.centerIn: parent
                                        name: navigationButton.modelData.icon
                                        color: navigationButton.checked ? "#84eff8"
                                            : navigationButton.hovered ? "#d8f4ff" : "#91a9c6"
                                        iconSize: navigationButton.checked ? 24 : 20
                                    }
                                }
                                Text {
                                    visible: !window.compactSidebar
                                    text: navigationButton.modelData.label
                                    color: navigationButton.checked ? "#f4f9ff"
                                        : navigationButton.hovered ? "#d8e7fa" : "#9bb0cf"
                                    font.pixelSize: 14
                                    font.weight: navigationButton.checked ? Font.DemiBold : Font.Medium
                                    elide: Text.ElideRight
                                    Layout.fillWidth: true
                                    Layout.minimumWidth: 0
                                }
                            }
                        }

                        background: Rectangle {
                            radius: theme.controlRadius
                            color: navigationButton.checked ? "#194464"
                                : navigationButton.down ? "#152a43"
                                : navigationButton.hovered ? "#142a43" : "transparent"
                            border.width: navigationButton.checked ? 1 : 0
                            border.color: "#317fa7"

                            Rectangle {
                                anchors.left: parent.left
                                anchors.verticalCenter: parent.verticalCenter
                                width: 3
                                height: 26
                                radius: 2
                                visible: navigationButton.checked
                                color: "#46d8ef"
                            }
                            Behavior on color { ColorAnimation { duration: theme.motionFast } }
                        }
                    }
                }

                Text {
                    visible: !window.compactSidebar
                    Layout.topMargin: 0
                    text: "下载 · 媒体 · 发布"
                    color: "#506f92"
                    font.pixelSize: 11
                }
            }
        }

        ColumnLayout {
            id: taskPage
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: 4
            visible: window.currentPage === 0

            UiPageTitle {
                Layout.fillWidth: true
                title: "下载任务"
                subtitle: window.narrowLayout
                    ? "添加、筛选并管理下载任务"
                    : "添加链接后自动解析；活动任务始终置顶。"
                iconName: "download"
            }

            Rectangle {
                id: commandDeck
                Layout.fillWidth: true
                implicitHeight: commandContent.implicitHeight + 12
                radius: theme.panelRadius
                color: theme.surface
                border.color: theme.border

                ColumnLayout {
                    id: commandContent
                    anchors.fill: parent
                    anchors.margins: 6
                    spacing: 4

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 4

                        LucideIcon { name: "circle-plus"; iconSize: 16; color: "#78e6f2" }
                        Text {
                            text: "新建下载"
                            color: "#dceeff"
                            font.pixelSize: 13
                            font.weight: Font.DemiBold
                        }
                        Text {
                            visible: !window.singleColumnLayout
                            text: "粘贴单条、合集或多条链接后自动解析"
                            color: "#7190b1"
                            font.pixelSize: 12
                        }
                        Item { Layout.fillWidth: true }
                        Text {
                            visible: !window.narrowLayout
                            text: "任务会按活动状态置顶"
                            color: "#6583a4"
                            font.pixelSize: 12
                        }
                    }

                    GridLayout {
                        Layout.fillWidth: true
                        columns: window.narrowLayout ? 2 : 4
                        columnSpacing: 4
                        rowSpacing: 4

                        UiTextField {
                            id: urlInput
                            objectName: "urlInput"
                            Layout.fillWidth: true
                            Layout.columnSpan: window.narrowLayout ? 2 : 1
                            Layout.preferredWidth: window.narrowLayout ? -1 : 350
                            leadingIcon: "clipboard-paste"
                            hintText: "粘贴视频或合集链接，支持多个链接"
                            onAccepted: {
                                if (appBridge.submitUrls(text))
                                    clear()
                            }
                        }
                        UiButton {
                            Layout.fillWidth: window.narrowLayout
                            text: "添加下载"
                            iconName: "circle-plus"
                            highlighted: true
                            onClicked: {
                                if (appBridge.submitUrls(urlInput.text))
                                    urlInput.clear()
                            }
                        }
                        UiTextField {
                            id: filterInput
                            objectName: "taskFilterInput"
                            Layout.fillWidth: true
                            leadingIcon: "search"
                            hintText: "搜索标题、链接或任务编号"
                            onTextEdited: filterDelay.restart()
                            onAccepted: {
                                filterDelay.stop()
                                appBridge.taskModel.setFilterText(text)
                            }
                        }
                        UiComboBox {
                            objectName: "taskSortMode"
                            Layout.fillWidth: window.narrowLayout
                            Layout.columnSpan: window.narrowLayout ? 2 : 1
                            Layout.preferredWidth: window.narrowLayout ? -1 : 132
                            model: ["最新优先", "最早优先", "标题"]
                            onActivated: appBridge.taskModel.setSortMode(
                                currentIndex === 1 ? "oldest" : currentIndex === 2 ? "title" : "newest"
                            )
                        }
                    }

                    UiComboBox {
                        id: taskStatusFilter
                        objectName: "taskStatusFilter"
                        Layout.preferredWidth: 154
                        property var statusOptions: [
                            { label: "全部状态", value: "all" },
                            { label: "进行中", value: "active" },
                            { label: "排队中", value: "queued" },
                            { label: "已暂停", value: "paused" },
                            { label: "处理中", value: "processing" },
                            { label: "已完成", value: "completed" },
                            { label: "文件缺失", value: "deleted" },
                            { label: "失败", value: "failed" },
                            { label: "已取消", value: "canceled" }
                        ]
                        model: statusOptions
                        textRole: "label"
                        currentIndex: {
                            for (let index = 0; index < statusOptions.length; ++index) {
                                if (statusOptions[index].value === appBridge.taskModel.statusFilter)
                                    return index
                            }
                            return 0
                        }
                        onActivated: function(index) {
                            appBridge.taskModel.setStatusFilter(statusOptions[index].value)
                        }
                    }

                    Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: "#213b58" }

                    Flow {
                        Layout.fillWidth: true
                        spacing: 4

                        UiButton {
                            checkable: true
                            checked: window.activeQuickAction === "folder"
                            text: "打开下载目录"
                            iconName: "folder-open"
                            iconSize: 15
                            onClicked: {
                                window.activeQuickAction = "folder"
                                appBridge.openDownloadDirectory()
                            }
                        }
                        UiButton {
                            text: "暂停全部"
                            iconName: "pause"
                            iconSize: 15
                            onClicked: {
                                window.activeQuickAction = ""
                                appBridge.pauseAllTasks()
                            }
                        }
                        UiButton {
                            text: "继续全部"
                            iconName: "play"
                            iconSize: 15
                            onClicked: {
                                window.activeQuickAction = ""
                                appBridge.resumeAllTasks()
                            }
                        }
                        UiButton {
                            text: "清理已完成"
                            iconName: "trash-2"
                            iconSize: 15
                            tone: "danger"
                            onClicked: {
                                window.activeQuickAction = ""
                                cleanupCompletedDialog.open()
                            }
                        }
                    }
                }
            }

            Rectangle {
                id: taskArea
                Layout.fillWidth: true
                Layout.fillHeight: true
                radius: theme.panelRadius
                color: theme.canvasRaised
                border.color: theme.border
                clip: true

                UiFlickable {
                    id: taskList
                    objectName: "taskList"
                    anchors.fill: parent
                    anchors.margins: 5
                    contentWidth: width
                    contentHeight: taskColumn.height
                    clip: true

                    Column {
                        id: taskColumn
                        width: taskList.width
                        height: implicitHeight
                        spacing: 4

                        Repeater {
                            model: appBridge.taskModel
                            delegate: TaskCard {
                                width: taskColumn.width
                                onDeleteRequested: function(requestTaskId) {
                                    deleteTaskDialog.taskId = requestTaskId
                                    deleteTaskDialog.open()
                                }
                                onLogRequested: function(requestTaskId) { taskLog.present(requestTaskId) }
                            }
                        }
                    }

                    ScrollBar.vertical: UiScrollBar { }
                }

                UiEmptyState {
                    anchors.centerIn: taskArea
                    width: Math.min(360, taskArea.width - 40)
                    visible: appBridge.taskModel.count === 0

                    property bool filtering: filterInput.text.length > 0
                                             || appBridge.taskModel.statusFilter !== "all"
                    iconName: filtering ? "search" : "download"
                    title: filtering ? "没有符合当前筛选条件的任务" : "还没有下载任务"
                    detail: taskArea.height < 140 ? "" : (filtering
                        ? "调整关键词或状态后再试。" : "粘贴视频或合集链接，即可开始下载。")
                    actionText: filtering || taskArea.height < 190 ? "" : "粘贴链接开始下载"
                    actionIcon: "clipboard-paste"
                    onActionRequested: urlInput.forceActiveFocus()
                }
            }
        }

        Loader {
            id: secondaryPageLoader
            Layout.fillWidth: true
            Layout.fillHeight: true
            active: window.currentPage !== 0
            visible: active
            sourceComponent: window.currentPage === 1 ? accountsPage
                : window.currentPage === 2 ? completedMediaPage
                : window.currentPage === 3 ? publishQueuePage
                : window.currentPage === 4 ? settingsPage
                : aboutPage

            // One transform-free transition per route keeps navigation tangible
            // without making long lists animate or compete for frame time.
            onLoaded: {
                opacity = 0
                pageContentEnter.restart()
            }

            NumberAnimation {
                id: pageContentEnter
                target: secondaryPageLoader
                property: "opacity"
                from: 0
                to: 1
                duration: theme.motionStandard
                easing.type: Easing.OutCubic
            }
        }
    }

    Rectangle {
        id: statusBar
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        anchors.leftMargin: theme.windowMargin
        anchors.rightMargin: theme.windowMargin
        anchors.bottomMargin: theme.windowMargin
        height: 28
        radius: theme.controlRadius
        color: theme.surface
        border.color: theme.border

        RowLayout {
            id: statusContent
            anchors.fill: parent
            anchors.leftMargin: 7
            anchors.rightMargin: 7
            spacing: 5

            LucideIcon {
                name: "download"
                iconSize: 14
                color: "#79e5f2"
            }
            Text {
                Layout.fillWidth: appBridge.message.length === 0
                Layout.minimumWidth: 0
                Layout.maximumWidth: appBridge.message.length > 0
                    ? Math.max(0, statusContent.width - 30) * 0.7
                    : Math.max(0, statusContent.width - 19)
                text: appBridge.statusSummary
                color: "#d5e7fb"
                font.pixelSize: 12
                elide: Text.ElideRight
                ToolTip.visible: truncated && summaryHover.hovered
                ToolTip.text: text
                ToolTip.delay: 500
                HoverHandler { id: summaryHover }
            }
            Rectangle {
                visible: appBridge.message.length > 0
                Layout.preferredWidth: 1
                Layout.fillHeight: true
                Layout.topMargin: 8
                Layout.bottomMargin: 8
                color: "#29415f"
            }
            Text {
                Layout.fillWidth: true
                Layout.minimumWidth: 0
                visible: appBridge.message.length > 0
                text: appBridge.message
                color: "#f8c577"
                font.pixelSize: 12
                elide: Text.ElideRight
                ToolTip.visible: truncated && messageHover.hovered
                ToolTip.text: text
                ToolTip.delay: 500
                HoverHandler { id: messageHover }
            }
        }
    }

    Component {
        id: accountsPage
        AccountsPage { }
    }

    Component {
        id: completedMediaPage
        CompletedPage { }
    }

    Component {
        id: publishQueuePage
        PublishQueuePage { }
    }

    Component {
        id: settingsPage
        SettingsPage { }
    }

    Component {
        id: aboutPage
        AboutPage { }
    }

}
