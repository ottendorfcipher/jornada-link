import CryptoKit
import Foundation

/// Mac-side mirror of everything sent to the device — the Swift twin of
/// `jornada/sendmirror.py`. Same tree layout, same `sent-manifest.jsonl`
/// format; keep the two implementations in sync.
///
/// Rationale: the Jornada's object store is battery-backed RAM, so every file
/// pushed to the device is also archived here, and prior versions are renamed
/// aside with a timestamp rather than overwritten.
public enum SendMirror {
    public static let manifestName = "sent-manifest.jsonl"

    public static var root: URL {
        if let override = ProcessInfo.processInfo.environment["JORNADA_MIRROR_DIR"],
           !override.trimmingCharacters(in: .whitespaces).isEmpty {
            return URL(fileURLWithPath: (override as NSString).expandingTildeInPath)
        }
        return FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Documents/Jornada Backup/Sent to Device")
    }

    /// One safe local path component from one device-path component; must match
    /// the Python `_local_name` rules exactly.
    public static func sanitize(_ name: String) -> String {
        var cleaned = name
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "\\", with: "_")
            .replacingOccurrences(of: ":", with: "_")
            .replacingOccurrences(of: "\0", with: "")
            .trimmingCharacters(in: .whitespaces)
        let dotless = cleaned.trimmingCharacters(in: CharacterSet(charactersIn: "."))
        if cleaned.isEmpty || cleaned == "." || cleaned == ".." || dotless.isEmpty {
            cleaned = "_" + (cleaned.isEmpty ? "unnamed" : cleaned)
        }
        return cleaned
    }

    /// Archive one successfully-sent payload; returns the mirror file URL.
    @discardableResult
    public static func archive(_ data: Data, devicePath: String, source: String?,
                        rootOverride: URL? = nil, date: Date = Date()) throws -> URL {
        let base = rootOverride ?? root
        var target = base
        let parts = devicePath.split(separator: "\\").map { sanitize(String($0)) }
        for part in parts.isEmpty ? ["_unnamed"] : parts {
            target.appendPathComponent(part)
        }
        let basePath = base.standardizedFileURL.path
        guard target.standardizedFileURL.path.hasPrefix(basePath + "/")
                || target.standardizedFileURL.path == basePath else {
            throw CocoaError(.fileWriteInvalidFileName)
        }
        let manager = FileManager.default
        try manager.createDirectory(at: target.deletingLastPathComponent(),
                                    withIntermediateDirectories: true)

        let digest = Insecure.MD5.hash(data: data).map { String(format: "%02x", $0) }.joined()
        var unchanged = false
        if manager.fileExists(atPath: target.path) {
            let existing = try Data(contentsOf: target)
            let existingDigest = Insecure.MD5.hash(data: existing)
                .map { String(format: "%02x", $0) }.joined()
            if existingDigest == digest {
                unchanged = true
            } else {
                try manager.moveItem(at: target, to: archivedName(for: target, date: date))
            }
        }
        if !unchanged {
            try data.write(to: target)
        }

        let stampFormatter = DateFormatter()
        stampFormatter.dateFormat = "yyyy-MM-dd HH:mm:ss"
        let entry: [String: Any] = [
            "ts": stampFormatter.string(from: date),
            "device_path": devicePath,
            "size": data.count,
            "md5": digest,
            "mirror": target.path,
            "unchanged": unchanged,
            "source": source ?? NSNull(),
        ]
        let line = try JSONSerialization.data(withJSONObject: entry) + Data("\n".utf8)
        let manifest = base.appendingPathComponent(manifestName)
        if let handle = try? FileHandle(forWritingTo: manifest) {
            defer { try? handle.close() }
            try handle.seekToEnd()
            try handle.write(contentsOf: line)
        } else {
            try line.write(to: manifest)
        }
        return target
    }

    private static func archivedName(for target: URL, date: Date) -> URL {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyyMMdd-HHmmss"
        let stamp = formatter.string(from: date)
        let ext = target.pathExtension
        let stem = target.deletingPathExtension().lastPathComponent
        let directory = target.deletingLastPathComponent()
        var counter = 0
        while true {
            let suffix = counter == 0 ? stamp : "\(stamp)-\(counter)"
            var candidate = directory.appendingPathComponent("\(stem).\(suffix)")
            if !ext.isEmpty { candidate.appendPathExtension(ext) }
            if !FileManager.default.fileExists(atPath: candidate.path) { return candidate }
            counter += 1
        }
    }
}
