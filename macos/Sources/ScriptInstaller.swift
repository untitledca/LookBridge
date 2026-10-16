import Foundation
import CryptoKit

struct InstallationReceipt: Codable {
    let appID: String
    let version: String
    let build: String
    let installedAt: Date
    let files: [String: String]
}

struct InstallerFailure: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

/// Owns exactly two marked directories; existing standalone Resolve scripts are untouched.
struct ScriptInstaller {
    static let appID = "com.atomfilmlab.lookbridge"
    static let markerName = ".lookbridge-managed.json"
    static let menuNames = ["LookBridge.py", "Reference Still Toggle.py"]
    let supportRoot: URL
    let backupRoot: URL
    let fm = FileManager.default
    // Injection is used only by the transaction rollback test, never by production callers.
    var beforeCommit: ((Int) throws -> Void)? = nil

    var menuDirectory: URL { supportRoot.appendingPathComponent("Fusion/Scripts/Utility/LookBridge") }
    var moduleDirectory: URL { supportRoot.appendingPathComponent("LookBridge/Modules") }
    var receiptURL: URL { supportRoot.appendingPathComponent("LookBridge/install-receipt.json") }

    static var currentUser: ScriptInstaller {
        let library = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support")
        return ScriptInstaller(supportRoot: library.appendingPathComponent("Blackmagic Design/DaVinci Resolve"),
                               backupRoot: library.appendingPathComponent("LookBridge/Backups"))
    }

    func receipt() -> InstallationReceipt? {
        guard let data = try? Data(contentsOf: receiptURL),
              let value = try? JSONDecoder().decode(InstallationReceipt.self, from: data),
              value.appID == Self.appID else { return nil }
        return value
    }

    func isCurrent(version: String, build: String) -> Bool {
        guard let record = receipt(), record.version == version, record.build == build else { return false }
        return [menuDirectory, moduleDirectory].allSatisfy { isManaged($0) }
            && record.files.keys.allSatisfy { fm.fileExists(atPath: moduleDirectory.appendingPathComponent($0).path) }
            && Self.menuNames.allSatisfy { fm.fileExists(atPath: menuDirectory.appendingPathComponent($0).path) }
    }

    func shouldAutomaticallyRefresh(version: String, build: String) -> Bool {
        guard let installed = receipt(), installed.build.compare(build, options: .numeric) != .orderedDescending else { return false }
        return !isCurrent(version: version, build: build)
    }

    func isManaged(_ directory: URL) -> Bool {
        guard let data = try? Data(contentsOf: directory.appendingPathComponent(Self.markerName)),
              let record = try? JSONDecoder().decode(InstallationReceipt.self, from: data) else { return false }
        return record.appID == Self.appID
    }

    private func rejectSymlink(_ url: URL) throws {
        if let type = try? fm.attributesOfItem(atPath: url.path)[.type] as? FileAttributeType,
           type == .typeSymbolicLink {
            throw InstallerFailure(message: "Installation stopped because \(url.path) is a symbolic link. Move that link aside and try again.")
        }
    }

    private func validateDestination(_ url: URL) throws {
        var current = url
        while current.path.hasPrefix(supportRoot.path), current.path != "/" {
            try rejectSymlink(current)
            if current == supportRoot { break }
            current.deleteLastPathComponent()
        }
        if fm.fileExists(atPath: url.path), !isManaged(url) {
            throw InstallerFailure(message: "An existing folder at \(url.path) was not installed by LookBridge. It has been left untouched. Move or rename it, then try again.")
        }
    }

    @discardableResult
    func install(from payload: URL, version: String, build: String) throws -> URL? {
        let sources = try fm.contentsOfDirectory(at: payload, includingPropertiesForKeys: [.isRegularFileKey, .isSymbolicLinkKey])
            .filter { $0.pathExtension == "py" }.sorted { $0.lastPathComponent < $1.lastPathComponent }
        let names = Set(sources.map(\.lastPathComponent))
        for required in Self.menuNames + ["CDL Auto Apply.py", "Reference Still Auto Overlay.py", "lookbridge_support.py", "reference_image.py", "comparison_capture.py", "psnr_guidance.py"] {
            guard names.contains(required) else { throw InstallerFailure(message: "The app is missing \(required). Download a complete LookBridge release.") }
        }
        var hashes: [String: String] = [:]
        for source in sources {
            let values = try source.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey])
            guard values.isRegularFile == true, values.isSymbolicLink != true else {
                throw InstallerFailure(message: "The script payload contains an unsupported file: \(source.lastPathComponent).")
            }
            hashes[source.lastPathComponent] = SHA256.hash(data: try Data(contentsOf: source)).map { String(format: "%02x", $0) }.joined()
        }
        for destination in [menuDirectory, moduleDirectory] { try validateDestination(destination) }
        try rejectSymlink(receiptURL)
        if fm.fileExists(atPath: receiptURL.path), receipt() == nil {
            throw InstallerFailure(message: "An unrecognized installation receipt already exists at \(receiptURL.path). It has been left untouched.")
        }

        let record = InstallationReceipt(appID: Self.appID, version: version, build: build, installedAt: Date(), files: hashes)
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        let recordData = try encoder.encode(record)
        let token = UUID().uuidString
        let destinations = [menuDirectory, moduleDirectory]
        let staged = destinations.map { $0.deletingLastPathComponent().appendingPathComponent(".LookBridge-stage-\(token)-\($0.lastPathComponent)") }
        let rollback = destinations.map { $0.deletingLastPathComponent().appendingPathComponent(".LookBridge-rollback-\(token)-\($0.lastPathComponent)") }
        let oldReceipt = try? Data(contentsOf: receiptURL)
        var installed = [Int]()
        var movedAside = [Int]()
        var backup: URL?
        defer { for stage in staged { try? fm.removeItem(at: stage) } }

        for directory in staged { try fm.createDirectory(at: directory, withIntermediateDirectories: true) }
        for source in sources {
            try fm.copyItem(at: source, to: staged[1].appendingPathComponent(source.lastPathComponent))
            if Self.menuNames.contains(source.lastPathComponent) {
                try fm.copyItem(at: source, to: staged[0].appendingPathComponent(source.lastPathComponent))
            }
        }
        for stage in staged { try recordData.write(to: stage.appendingPathComponent(Self.markerName), options: .atomic) }
        // A durable backup is complete before any currently installed script is replaced.
        if destinations.contains(where: { fm.fileExists(atPath: $0.path) }) {
            let directory = backupRoot.appendingPathComponent("\(Int(Date().timeIntervalSince1970))-\(token)")
            try fm.createDirectory(at: directory, withIntermediateDirectories: true)
            for (index, destination) in destinations.enumerated() where fm.fileExists(atPath: destination.path) {
                try fm.copyItem(at: destination, to: directory.appendingPathComponent(index == 0 ? "Menu" : "Modules"))
            }
            if let oldReceipt { try oldReceipt.write(to: directory.appendingPathComponent("install-receipt.json"), options: .atomic) }
            backup = directory
        }
        do {
            for (index, destination) in destinations.enumerated() {
                try beforeCommit?(index)
                if fm.fileExists(atPath: destination.path) {
                    try fm.moveItem(at: destination, to: rollback[index])
                    movedAside.append(index)
                }
                try fm.moveItem(at: staged[index], to: destination)
                installed.append(index)
            }
            try recordData.write(to: receiptURL, options: .atomic)
        } catch {
            var rollbackErrors = [String]()
            for index in installed.reversed() {
                do { try fm.removeItem(at: destinations[index]) } catch { rollbackErrors.append(error.localizedDescription) }
            }
            for index in movedAside.reversed() {
                do { try fm.moveItem(at: rollback[index], to: destinations[index]) } catch { rollbackErrors.append(error.localizedDescription) }
            }
            if let oldReceipt { try? oldReceipt.write(to: receiptURL, options: .atomic) }
            else { try? fm.removeItem(at: receiptURL) }
            if !rollbackErrors.isEmpty {
                throw InstallerFailure(message: "Installation failed: \(error.localizedDescription). Restore files from \(backup?.path ?? "the .LookBridge-rollback folders beside the installation"). Rollback details: \(rollbackErrors.joined(separator: "; "))")
            }
            throw error
        }
        for directory in rollback { try? fm.removeItem(at: directory) }
        return backup
    }
}
