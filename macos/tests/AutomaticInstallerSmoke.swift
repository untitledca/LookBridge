import AppKit
import Foundation

/// Runs the production updater and script refresh model against temporary data only.
@MainActor
final class SmokeDelegate: NSObject, NSApplicationDelegate {
    private let updates = AppUpdater()
    private var windowVisible = true
    private var resolveRunning = true
    private var scheduled = false
    private var failed = false
    private var timer: Timer?
    private var deadline: Timer?
    private var model: AppModel?
    private var priorStatus = ""
    private var version: String { Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as! String }
    private var logURL: URL { URL(fileURLWithPath: Bundle.main.object(forInfoDictionaryKey: "SmokeLogPath") as! String) }
    private var root: URL { URL(fileURLWithPath: Bundle.main.object(forInfoDictionaryKey: "SmokeRoot") as! String) }
    private var installer: ScriptInstaller { ScriptInstaller(supportRoot: root.appendingPathComponent("Resolve"), backupRoot: root.appendingPathComponent("Backups")) }

    func applicationDidFinishLaunching(_ notification: Notification) {
        record("launched")
        if version == "2" {
            record("replacementRelaunched")
            model = AppModel(installer: installer, resolveIsRunning: { [weak self] in self?.resolveRunning ?? true })
            DispatchQueue.main.asyncAfter(deadline: .now() + 1) {
                self.record("scriptRefreshDeferred", extra: ["build": self.installer.receipt()?.build ?? "missing", "installed": self.model?.installed ?? true])
                self.resolveRunning = false
                DispatchQueue.main.asyncAfter(deadline: .now() + 3) {
                    self.record("scriptsRefreshed", extra: ["build": self.installer.receipt()?.build ?? "missing", "installed": self.model?.installed ?? false])
                    NSApp.terminate(nil)
                }
            }
            return
        }
        do { try installer.install(from: Bundle.main.resourceURL!.appendingPathComponent("Scripts"), version: "1.0", build: "1") }
        catch { record("testError", extra: ["error": error.localizedDescription]); NSApp.terminate(nil); return }
        updates.mayInstallAutomatically = { [weak self] in
            guard let self else { return false }
            return !self.windowVisible && !self.resolveRunning
        }
        updates.setAutomaticInstallation(true)
        updates.start()
        timer = Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { [weak self] _ in MainActor.assumeIsolated { self?.poll() } }
        deadline = Timer.scheduledTimer(withTimeInterval: 100, repeats: false) { [weak self] _ in
            MainActor.assumeIsolated { self?.record("timedOut", extra: ["status": self?.updates.status ?? "unknown"]); NSApp.terminate(nil) }
        }
    }
    private func poll() {
        if updates.status != priorStatus { priorStatus = updates.status; record("status", extra: ["status": updates.status]) }
        if updates.status.hasPrefix("Update could not finish:"), !failed {
            failed = true
            record("updateRejected", extra: ["error": updates.status])
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { NSApp.terminate(nil) }
        }
        updates.reevaluate()
        guard updates.pending, !scheduled else { return }
        scheduled = true
        record("downloadValidatedAndPrepared")
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) {
            self.record("windowDeferralVerified", extra: ["pending": self.updates.pending, "stillOriginalVersion": self.version == "1"])
            self.windowVisible = false
            self.updates.reevaluate()
            DispatchQueue.main.asyncAfter(deadline: .now() + 1) {
                self.record("resolveDeferralVerified", extra: ["pending": self.updates.pending, "stillOriginalVersion": self.version == "1"])
                self.resolveRunning = false
                self.record("safeToInstall")
                // The periodic state refresh must notice Resolve quit after the window closed.
            }
        }
    }
    private func record(_ event: String, extra: [String: Any] = [:]) {
        var row = extra
        row["event"] = event; row["version"] = version; row["pid"] = ProcessInfo.processInfo.processIdentifier
        guard var data = try? JSONSerialization.data(withJSONObject: row, options: [.sortedKeys]) else { return }
        data.append(0x0a)
        if !FileManager.default.fileExists(atPath: logURL.path) { FileManager.default.createFile(atPath: logURL.path, contents: nil) }
        guard let file = try? FileHandle(forWritingTo: logURL) else { return }
        defer { try? file.close() }
        try? file.seekToEnd(); try? file.write(contentsOf: data)
    }
}
@main struct Smoke {
    @MainActor static func main() {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        let delegate = SmokeDelegate(); app.delegate = delegate
        withExtendedLifetime(delegate) { app.run() }
    }
}
