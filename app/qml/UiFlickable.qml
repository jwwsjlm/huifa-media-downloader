import QtQuick

Flickable {
    id: root

    property real wheelStepLimit: 28
    property real pendingWheelDistance: 0

    boundsBehavior: Flickable.DragAndOvershootBounds
    boundsMovement: Flickable.FollowBoundsBehavior
    flickableDirection: Flickable.VerticalFlick
    synchronousDrag: true
    interactive: contentHeight > height
    maximumFlickVelocity: 1150
    flickDeceleration: 4800
    pixelAligned: true

    // A short rebound makes the edge of long lists feel anchored without
    // delaying scrolling or moving the surrounding layout.
    rebound: Transition {
        SpringAnimation {
            properties: "x,y"
            spring: 4.0
            damping: 0.58
            epsilon: 0.1
        }
    }

    function scrollBy(rawDistance) {
        const distance = Math.max(-wheelStepLimit, Math.min(wheelStepLimit, rawDistance))
        pendingWheelDistance += distance
        wheelApply.restart()
    }

    Timer {
        id: wheelApply
        interval: 0
        repeat: false
        onTriggered: {
            const distance = Math.max(
                -root.wheelStepLimit,
                Math.min(root.wheelStepLimit, root.pendingWheelDistance)
            )
            root.pendingWheelDistance = 0
            const maximumY = Math.max(0, root.contentHeight - root.height)
            wheelMotion.stop()
            wheelMotion.from = root.contentY
            wheelMotion.to = Math.max(0, Math.min(maximumY, root.contentY - distance))
            wheelMotion.start()
        }
    }

    PropertyAnimation {
        id: wheelMotion
        target: root
        property: "contentY"
        duration: 90
        easing.type: Easing.OutCubic
    }

    /*
       Delay the write until the wheel event finishes propagating through
       Flickable.  This keeps Qt's native handler from overwriting the target
       in the same event turn and lets rapid wheel ticks coalesce cleanly.
    */

    WheelHandler {
        target: null
        enabled: root.interactive
        onWheel: function(event) {
            const rawDistance = event.pixelDelta.y !== 0
                ? event.pixelDelta.y : event.angleDelta.y * 0.16
            root.scrollBy(rawDistance)
            event.accepted = true
        }
    }
}
