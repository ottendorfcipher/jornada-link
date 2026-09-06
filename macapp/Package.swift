// swift-tools-version:5.10
import PackageDescription

let package = Package(
    name: "JornadaSync",
    platforms: [.macOS("15.0")],
    products: [
        .library(name: "JornadaCore", targets: ["JornadaCore"]),
        .executable(name: "JornadaSync", targets: ["JornadaSync"]),
        .executable(name: "IconGen", targets: ["IconGen"]),
        .executable(name: "SelfTest", targets: ["SelfTest"]),
    ],
    targets: [
        .target(name: "JornadaCore"),
        .executableTarget(name: "JornadaSync", dependencies: ["JornadaCore"]),
        .executableTarget(name: "IconGen", dependencies: ["JornadaCore"]),
        .executableTarget(name: "SelfTest", dependencies: ["JornadaCore"]),
    ]
)
