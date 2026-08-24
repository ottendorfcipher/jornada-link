// swift-tools-version:5.10
import PackageDescription

let package = Package(
    name: "JornadaSync",
    platforms: [.macOS(.v14)],
    products: [
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
