import AppKit
import Foundation

@main
struct AppModelTests {
    @MainActor static func main() throws {
        let fm = FileManager.default
        let work = fm.temporaryDirectory.appendingPathComponent("lookbridge-model-tests-\(UUID().uuidString)")
        defer { try? fm.removeItem(at: work) }
        let installer = ScriptInstaller(supportRoot: work.appendingPathComponent("Resolve"), backupRoot: work.appendingPathComponent("Backups"))
        try installer.install(from: URL(fileURLWithPath: CommandLine.arguments[1]), version: "2.0.0", build: "2")
        var running = false
        let model = AppModel(installer: installer, resolveIsRunning: { running })
        precondition(installer.receipt()?.build == "2" && !model.failed && !model.updateSyncAttempted)
        precondition(model.status.contains("Newer LookBridge scripts"))
        print("PASS: launching an old app keeps the newer script receipt and reports the newer version")
        precondition(!model.resolveRunning)
        running = true
        model.install()
        precondition(model.resolveRunning && model.status.contains("Quit DaVinci Resolve"))
        precondition(installer.receipt()?.build == "2")
        print("PASS: install button rechecks Resolve immediately even before the periodic status refresh")
        model.timer?.invalidate()
    }
}
