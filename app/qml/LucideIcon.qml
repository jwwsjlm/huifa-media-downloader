import QtQuick

Item {
    id: root

    property string name: "circle"
    property color color: "#abc0da"
    property int iconSize: 18
    property real strokeWidth: 2

    implicitWidth: iconSize
    implicitHeight: iconSize
    width: iconSize
    height: iconSize

    readonly property string paths: {
        switch (name) {
        case "download": return '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" x2="12" y1="15" y2="3"/>'
        case "user-round": return '<circle cx="12" cy="8" r="5"/><path d="M20 21a8 8 0 0 0-16 0"/>'
        case "library": return '<path d="m16 6 4 14"/><path d="M12 6v14"/><path d="M8 8v12"/><path d="M4 4v16"/>'
        case "send": return '<path d="m22 2-7 20-4-9-9-4Z"/><path d="M22 2 11 13"/>'
        case "settings-2": return '<path d="M20 7h-9"/><path d="M14 17H5"/><circle cx="17" cy="17" r="3"/><circle cx="7" cy="7" r="3"/>'
        case "circle-help": return '<circle cx="12" cy="12" r="10"/><path d="M9.1 9a3 3 0 1 1 5.83 1c0 2-3 2-3 4"/><path d="M12 17h.01"/>'
        case "search": return '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>'
        case "plus": return '<path d="M5 12h14"/><path d="M12 5v14"/>'
        case "folder-open": return '<path d="m6 14 1.5-4.5A2 2 0 0 1 9.4 8H20a2 2 0 0 1 1.9 2.6l-1.3 4A2 2 0 0 1 18.7 16H4.2a2 2 0 0 1-1.9-2.6l1.3-4A2 2 0 0 1 5.5 8H14l2-3H5a2 2 0 0 0-2 2v9"/>'
        case "pause": return '<rect x="14" y="4" width="4" height="16" rx="1"/><rect x="6" y="4" width="4" height="16" rx="1"/>'
        case "play": return '<polygon points="5 3 19 12 5 21 5 3"/>'
        case "trash-2": return '<path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/>'
        case "refresh-cw": return '<path d="M21 12a9 9 0 0 0-15.5-6.2L3 8"/><path d="M3 3v5h5"/><path d="M3 12a9 9 0 0 0 15.5 6.2L21 16"/><path d="M16 16h5v5"/>'
        case "check-circle-2": return '<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><path d="m9 11 3 3L22 4"/>'
        case "list-filter": return '<path d="M3 6h18"/><path d="M7 12h10"/><path d="M10 18h4"/>'
        case "video": return '<path d="m16 13 5 3V8l-5 3"/><rect x="3" y="6" width="13" height="12" rx="2"/>'
        case "image": return '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="m21 15-5-5L5 21"/>'
        case "sparkles": return '<path d="m12 3-1.9 5.8L4.5 10.7l5.6 1.9L12 18.5l1.9-5.9 5.6-1.9-5.6-1.9Z"/><path d="m19 3 .8 2.2L22 6l-2.2.8L19 9l-.8-2.2L16 6l2.2-.8Z"/><path d="m5 16 .7 1.8L7.5 19l-1.8.7L5 21.5l-.7-1.8L2.5 19l1.8-.7Z"/>'
        case "info": return '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/>'
        case "log-in": return '<path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"/><polyline points="10 17 15 12 10 7"/><line x1="15" x2="3" y1="12" y2="12"/>'
        case "external-link": return '<path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>'
        case "copy": return '<rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>'
        case "x": return '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>'
        case "file-text": return '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v6h6"/><path d="M8 13h8"/><path d="M8 17h8"/><path d="M8 9h2"/>'
        case "upload": return '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" x2="12" y1="3" y2="15"/>'
        case "circle-alert": return '<circle cx="12" cy="12" r="10"/><line x1="12" x2="12" y1="8" y2="12"/><line x1="12" x2="12.01" y1="16" y2="16"/>'
        case "chevron-down": return '<path d="m6 9 6 6 6-6"/>'
        case "chevron-up": return '<path d="m18 15-6-6-6 6"/>'
        case "chevron-right": return '<path d="m9 18 6-6-6-6"/>'
        case "chevron-left": return '<path d="m15 18-6-6 6-6"/>'
        case "check": return '<path d="m5 12 5 5L20 7"/>'
        case "ellipsis": return '<circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/><circle cx="5" cy="12" r="1"/>'
        case "sliders-horizontal": return '<line x1="21" x2="14" y1="4" y2="4"/><line x1="10" x2="3" y1="4" y2="4"/><line x1="21" x2="12" y1="12" y2="12"/><line x1="8" x2="3" y1="12" y2="12"/><line x1="21" x2="16" y1="20" y2="20"/><line x1="12" x2="3" y1="20" y2="20"/><line x1="14" x2="14" y1="2" y2="6"/><line x1="8" x2="8" y1="10" y2="14"/><line x1="16" x2="16" y1="18" y2="22"/>'
        case "circle-plus": return '<circle cx="12" cy="12" r="10"/><path d="M8 12h8"/><path d="M12 8v8"/>'
        case "clipboard-paste": return '<path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><rect width="8" height="4" x="8" y="2" rx="1" ry="1"/><path d="M9 14h6"/><path d="M9 18h4"/>'
        case "list-checks": return '<path d="m3 17 2 2 4-4"/><path d="m3 7 2 2 4-4"/><path d="M13 6h8"/><path d="M13 12h8"/><path d="M13 18h8"/>'
        default: return '<circle cx="12" cy="12" r="8"/>'
        }
    }

    readonly property string svg: '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="' + root.color.toString() + '" stroke-width="' + root.strokeWidth + '" stroke-linecap="round" stroke-linejoin="round">' + root.paths + '</svg>'

    Image {
        anchors.centerIn: parent
        width: root.iconSize
        height: root.iconSize
        // Hidden icon slots (for example text-only buttons) must not decode a
        // throwaway SVG for every delegate in a long, non-virtualized list.
        source: root.visible ? "data:image/svg+xml;utf8," + encodeURIComponent(root.svg) : ""
        fillMode: Image.PreserveAspectFit
        smooth: true
        asynchronous: true
        cache: true
    }
}
