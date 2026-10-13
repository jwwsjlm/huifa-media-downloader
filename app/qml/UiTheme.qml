import QtQuick

QtObject {
    readonly property color canvas: "#08111f"
    readonly property color canvasRaised: "#0c1729"
    readonly property color surface: "#0f1c2e"
    readonly property color surfaceRaised: "#14253a"
    readonly property color surfaceHover: "#192d46"
    readonly property color surfaceSelected: "#173b59"
    readonly property color field: "#0b1728"
    readonly property color popup: "#0d1828"
    readonly property color border: "#27425f"
    readonly property color borderStrong: "#386489"
    readonly property color text: "#f3f8ff"
    readonly property color textSecondary: "#abc0da"
    readonly property color textMuted: "#7891b0"
    readonly property color primary: "#20c4df"
    readonly property color primaryStrong: "#0e95bc"
    readonly property color primarySurface: "#123f5b"
    readonly property color success: "#47d497"
    readonly property color warning: "#f6bd60"
    readonly property color danger: "#fb7890"
    readonly property color dangerSurface: "#351d2c"
    readonly property color focus: "#5de1f2"
    readonly property color disabledSurface: "#122037"
    readonly property color disabledBorder: "#223955"

    // The rail follows the window. Task controls follow the actual remaining
    // content width, so the layout does not wrap merely because the rail is open.
    readonly property int defaultWindowWidth: 920
    readonly property int defaultWindowHeight: 580
    readonly property int minimumWindowWidth: 640
    readonly property int minimumWindowHeight: 460
    readonly property int windowMargin: 5
    readonly property int windowGap: 5
    readonly property int controlHeight: 32
    readonly property int primaryControlHeight: 36
    readonly property int controlRadius: 5
    readonly property int panelRadius: 6
    readonly property int popupRadius: 6
    readonly property int controlHorizontalPadding: 16
    readonly property int iconSmall: 14
    readonly property int iconMedium: 16
    readonly property int motionPress: 75
    readonly property int motionFast: 100
    readonly property int motionStandard: 140
    readonly property int motionExpand: 150
    // Keep the rail compact until both navigation labels and task controls fit.
    readonly property int wideBreakpoint: 1100
    readonly property int taskWideContentBreakpoint: 800
    readonly property int taskSingleColumnContentBreakpoint: 650
    readonly property int wideSidebarWidth: 152
    readonly property int compactSidebarWidth: 50
}
