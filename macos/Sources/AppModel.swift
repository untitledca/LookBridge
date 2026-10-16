import AppKit
import Combine

@MainActor
final class AppModel: ObservableObject {
    @Published var installed = false
    @Published var busy = false
    @Published var resolveRunning = false
    @Published var status = "Install both tools, then open LookBridge from Resolve’s Workspace → Scripts → LookBridge menu."
    @Published var failed = false
    let version = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "1.0.0"
    let build = Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "1"
    let installer: ScriptInstaller
    private let resolveIsRunning: () -> Bool
    var timer: Timer?
    var updateSyncAttempted = false
    var onStateRefresh: (() -> Void)?

    init(installer: ScriptInstaller = .currentUser, resolveIsRunning: @escaping () -> Bool = {
        NSWorkspace.shared.runningApplications.contains {
            $0.bundleIdentifier?.lowercased().contains("davinciresolve") == true || $0.executableURL?.lastPathComponent == "Resolve"
        }
    }) {
        self.installer = installer
        self.resolveIsRunning = resolveIsRunning
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.refresh() }
        }
    }
    private func refresh() {
        defer { onStateRefresh?() }
        resolveRunning = resolveIsRunning()
        installed = installer.isCurrent(version: version, build: build)
        guard !busy, !updateSyncAttempted else { return }
        if let previous = installer.receipt(), previous.build.compare(build, options: .numeric) == .orderedDescending {
            status = "Newer LookBridge scripts (\(previous.version)) are already installed. Check for updates to update this app. Clicking Install will replace them with v\(version)."
            return
        }
        guard installer.shouldAutomaticallyRefresh(version: version, build: build) else { return }
        if resolveRunning {
            status = "A script refresh is ready. Quit DaVinci Resolve to refresh its scripts safely; this window will finish automatically."
        } else {
            updateSyncAttempted = true
            installScripts()
        }
    }
    func install() {
        guard !busy else { return }
        resolveRunning = resolveIsRunning()
        guard !resolveRunning else {
            status = "Quit DaVinci Resolve, then click Install. This keeps an open tool from mixing old and new helper files."
            failed = false
            return
        }
        busy = true; failed = false
        do {
            let copiedApp = try installManagerIfNeeded()
            try applyScripts()
            busy = false
            if let copiedApp {
                let configuration = NSWorkspace.OpenConfiguration()
                configuration.arguments = ["--post-install"]
                // The DMG copy is still running with the same bundle identifier.
                // Launch the installed path explicitly instead of reactivating that process.
                configuration.createsNewApplicationInstance = true
                NSWorkspace.shared.openApplication(at: copiedApp, configuration: configuration) { _, error in
                    DispatchQueue.main.async {
                        if let error { self.status += " Open \(copiedApp.path) to enable updates: \(error.localizedDescription)" }
                        else { NSApp.terminate(nil) }
                    }
                }
            }
        } catch { status = error.localizedDescription; failed = true; busy = false }
    }
    private func installScripts() {
        busy = true; failed = false
        do { try applyScripts() }
        catch { status = "Could not refresh scripts: \(error.localizedDescription)"; failed = true }
        busy = false
    }
    private func applyScripts() throws {
        resolveRunning = resolveIsRunning()
        guard !resolveRunning else {
            throw InstallerFailure(message: "DaVinci Resolve is open. Quit Resolve before installing or refreshing its scripts.")
        }
        guard let payload = Bundle.main.resourceURL?.appendingPathComponent("Scripts") else {
            throw InstallerFailure(message: "The app’s bundled scripts could not be found.")
        }
        _ = try installer.install(from: payload, version: version, build: build)
        installed = true
        status = "Installed for your account. Open Resolve Studio, then choose Workspace → Scripts → LookBridge → LookBridge."
    }
    private func installManagerIfNeeded() throws -> URL? {
        let app = Bundle.main.bundleURL
        let userApps = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Applications")
        if app.deletingLastPathComponent() == userApps || app.path.hasPrefix("/Applications/") { return nil }
        let fm = FileManager.default
        try fm.createDirectory(at: userApps, withIntermediateDirectories: true)
        let destination = userApps.appendingPathComponent("LookBridge.app")
        if fm.fileExists(atPath: destination.path), Bundle(url: destination)?.bundleIdentifier != ScriptInstaller.appID {
            throw InstallerFailure(message: "A different app already exists at \(destination.path). Move it aside before installing LookBridge.")
        }
        let staging = userApps.appendingPathComponent(".LookBridge-stage-\(UUID().uuidString).app")
        let previous = userApps.appendingPathComponent(".LookBridge-previous-\(UUID().uuidString).app")
        defer { try? fm.removeItem(at: staging) }
        try fm.copyItem(at: app, to: staging)
        let hadPrevious = fm.fileExists(atPath: destination.path)
        if hadPrevious { try fm.moveItem(at: destination, to: previous) }
        do { try fm.moveItem(at: staging, to: destination) }
        catch { if hadPrevious { try? fm.moveItem(at: previous, to: destination) }; throw error }
        if hadPrevious { try? fm.removeItem(at: previous) }
        return destination
    }
    func showScripts() { NSWorkspace.shared.open(installer.menuDirectory) }
    func openResolve() {
        let possible = ["/Applications/DaVinci Resolve/DaVinci Resolve.app", "/Applications/DaVinci Resolve Studio/DaVinci Resolve Studio.app"]
        if let path = possible.first(where: { FileManager.default.fileExists(atPath: $0) }) {
            NSWorkspace.shared.open(URL(fileURLWithPath: path))
        } else { status = "Open DaVinci Resolve Studio from your Applications folder to use the installed scripts." }
    }
}
