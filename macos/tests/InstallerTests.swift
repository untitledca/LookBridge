import Foundation

@main
struct InstallerTests {
    static func main() throws {
        let fm = FileManager.default
        let work = fm.temporaryDirectory.appendingPathComponent("lookbridge-installer-tests-\(UUID().uuidString)")
        try fm.createDirectory(at: work, withIntermediateDirectories: true)
        defer { try? fm.removeItem(at: work) }
        let payload = URL(fileURLWithPath: CommandLine.arguments[1])
        let installer = ScriptInstaller(supportRoot: work.appendingPathComponent("Resolve"), backupRoot: work.appendingPathComponent("Backups"))
        func expect(_ condition: @autoclosure () -> Bool, _ label: String) {
            guard condition() else { fatalError("FAILED: \(label)") }
            print("PASS: \(label)")
        }
        let unrelated = installer.menuDirectory.deletingLastPathComponent().appendingPathComponent("My Script.py")
        try fm.createDirectory(at: unrelated.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data("# user script".utf8).write(to: unrelated)
        let backup = try installer.install(from: payload, version: "1.0.0", build: "1")
        expect(backup == nil, "fresh install does not create an unnecessary backup")
        expect(installer.isCurrent(version: "1.0.0", build: "1"), "fresh install has complete receipt and both directories")
        let menu = try fm.contentsOfDirectory(atPath: installer.menuDirectory.path).sorted()
        expect(menu == [".lookbridge-managed.json", "LookBridge.py", "Reference Still Toggle.py"], "only two launchers are exposed in Resolve menu")
        let original = try Data(contentsOf: unrelated)
        expect(original == Data("# user script".utf8), "unrelated Resolve script is preserved")
        let modified = installer.moduleDirectory.appendingPathComponent("CDL Auto Apply.py")
        try Data("# locally modified".utf8).write(to: modified)
        let backup2 = try installer.install(from: payload, version: "1.0.1", build: "2")!
        let saved = try Data(contentsOf: backup2.appendingPathComponent("Modules/CDL Auto Apply.py"))
        expect(saved == Data("# locally modified".utf8), "user changes are backed up before replacement")
        expect(installer.isCurrent(version: "1.0.1", build: "2"), "updated receipt records new build")
        expect(!installer.shouldAutomaticallyRefresh(version: "1.0.0", build: "1"), "opening an older app never automatically downgrades installed scripts")
        let before = try Data(contentsOf: installer.receiptURL)
        var failing = installer
        failing.beforeCommit = { index in if index == 1 { throw InstallerFailure(message: "simulated second-directory failure") } }
        do { try failing.install(from: payload, version: "1.0.2", build: "3"); fatalError("Expected transaction failure") }
        catch { expect(error.localizedDescription == "simulated second-directory failure", "injected commit failure is reported") }
        let after = try Data(contentsOf: installer.receiptURL)
        expect(before == after && installer.isCurrent(version: "1.0.1", build: "2"), "failed second commit restores directories and original receipt")
        let removed = installer.moduleDirectory.appendingPathComponent("reference_image.py")
        try fm.removeItem(at: removed)
        expect(!installer.isCurrent(version: "1.0.1", build: "2"), "missing helper is detected for repair")
        let collision = ScriptInstaller(supportRoot: work.appendingPathComponent("Collision"), backupRoot: work.appendingPathComponent("CollisionBackups"))
        try fm.createDirectory(at: collision.menuDirectory, withIntermediateDirectories: true)
        let userFile = collision.menuDirectory.appendingPathComponent("User.py")
        try Data("owned by user".utf8).write(to: userFile)
        do { try collision.install(from: payload, version: "1", build: "1"); fatalError("Expected ownership failure") }
        catch { expect(fm.fileExists(atPath: userFile.path), "unowned folder conflict is rejected without modification") }
        let linked = ScriptInstaller(supportRoot: work.appendingPathComponent("Linked"), backupRoot: work.appendingPathComponent("LinkedBackups"))
        try fm.createDirectory(at: linked.menuDirectory.deletingLastPathComponent(), withIntermediateDirectories: true)
        try fm.createSymbolicLink(at: linked.menuDirectory, withDestinationURL: installer.menuDirectory)
        do { try linked.install(from: payload, version: "1", build: "1"); fatalError("Expected symlink failure") }
        catch { expect(error.localizedDescription.contains("symbolic link"), "symlink destination is rejected") }
        let invalidPayload = work.appendingPathComponent("Incomplete")
        try fm.copyItem(at: payload, to: invalidPayload)
        try fm.removeItem(at: invalidPayload.appendingPathComponent("psnr_guidance.py"))
        do { try installer.install(from: invalidPayload, version: "1", build: "1"); fatalError("Expected missing helper failure") }
        catch { expect(error.localizedDescription.contains("psnr_guidance.py"), "incomplete payload is rejected before touching installed files") }
        print("All installer checks passed in a temporary directory. Real Resolve installation untouched.")
    }
}
