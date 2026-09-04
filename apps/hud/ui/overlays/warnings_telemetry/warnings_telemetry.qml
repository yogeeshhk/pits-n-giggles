import QtQuick
import QtQuick.Window

Window {
    id: root
    visible: true
    color: "transparent"
    flags: Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint

    property real scaleFactor: 1.0
    property int cornerCuttingWarnings: 0
    property int trackLimitsWarnings: 0

    readonly property int baseWidth: 180
    readonly property int baseHeight: 38

    width: Math.max(1, Math.round(baseWidth * scaleFactor))
    height: Math.max(1, Math.round(baseHeight * scaleFactor))

    Item {
        anchors.centerIn: parent
        width: root.baseWidth
        height: root.baseHeight

        transform: Scale {
            xScale: root.scaleFactor
            yScale: root.scaleFactor
            origin.x: root.baseWidth / 2
            origin.y: root.baseHeight / 2
        }

        Column {
            anchors.centerIn: parent
            spacing: 0

            Text {
                width: root.baseWidth
                text: "Corner Cutting " + root.cornerCuttingWarnings
                font.family: "Consolas"
                font.pixelSize: 16
                font.bold: true
                color: "#00e676"
                horizontalAlignment: Text.AlignHCenter
            }

            Text {
                width: root.baseWidth
                text: "Track Limits " + root.trackLimitsWarnings
                font.family: "Consolas"
                font.pixelSize: 16
                font.bold: true
                color: "#00e676"
                horizontalAlignment: Text.AlignHCenter
            }
        }
    }
}
