import AppKit
import Combine
import Sparkle

@MainActor
final class AppUpdater: NSObject, ObservableObject, SPUUpdaterDelegate {
    @Published var checks = false
    @Published var automaticInstallation = false
    @Published var canCheck = false
    @Published var status = "Updates are optional. Check whenever you like."
    @Published var pending = false
    var mayInstallAutomatically: () -> Bool = { false }
    private var controller: SPUStandardUpdaterController!
    private var observations = [NSKeyValueObservation]()
    private var installation: (() -> Void)?
    private var started = false

    override init() {
        super.init()
        controller = SPUStandardUpdaterController(startingUpdater: false, updaterDelegate: self, userDriverDelegate: nil)
        observations = [
            controller.updater.observe(\.automaticallyChecksForUpdates, options: [.initial, .new]) { [weak self] _, _ in MainActor.assumeIsolated { self?.refresh() } },
            controller.updater.observe(\.automaticallyDownloadsUpdates, options: [.initial, .new]) { [weak self] _, _ in MainActor.assumeIsolated { self?.refresh() } },
            controller.updater.observe(\.canCheckForUpdates, options: [.initial, .new]) { [weak self] _, _ in MainActor.assumeIsolated { self?.refresh() } }
        ]
    }
    func start() {
        guard !started else { return }
        do { try controller.updater.start(); started = true; refresh() }
        catch { status = "Updates unavailable: \(error.localizedDescription)" }
    }
    func check() { if canCheck { controller.checkForUpdates(nil) } }
    func setChecks(_ enabled: Bool) {
        if !enabled { controller.updater.automaticallyDownloadsUpdates = false }
        controller.updater.automaticallyChecksForUpdates = enabled
        refresh()
    }
    func setAutomaticInstallation(_ enabled: Bool) {
        if enabled { controller.updater.automaticallyChecksForUpdates = true }
        controller.updater.automaticallyDownloadsUpdates = enabled
        refresh()
    }
    private func refresh() {
        checks = controller.updater.automaticallyChecksForUpdates
        automaticInstallation = controller.updater.automaticallyDownloadsUpdates
        canCheck = started && controller.updater.canCheckForUpdates
        reevaluate()
    }
    func reevaluate() {
        if checks && automaticInstallation && mayInstallAutomatically() { installPending() }
    }
    func installPending() {
        guard let action = installation else { return }
        installation = nil; pending = false
        status = "Installing and relaunching LookBridge…"
        action()
    }
    func updater(_ updater: SPUUpdater, mayPerform updateCheck: SPUUpdateCheck) throws { status = "Checking for updates…" }
    func updater(_ updater: SPUUpdater, didFindValidUpdate item: SUAppcastItem) { status = "LookBridge \(item.displayVersionString) is available." }
    func updaterDidNotFindUpdate(_ updater: SPUUpdater, error: Error) { status = "No newer compatible version is available." }
    func updater(_ updater: SPUUpdater, willDownloadUpdate item: SUAppcastItem, with request: NSMutableURLRequest) { status = "Downloading and verifying the update…" }
    func updater(_ updater: SPUUpdater, willInstallUpdateOnQuit item: SUAppcastItem, immediateInstallationBlock block: @escaping () -> Void) -> Bool {
        installation = block; pending = true
        status = "Update ready. Install now, or it will install when LookBridge quits."
        DispatchQueue.main.async { [weak self] in self?.reevaluate() }
        return true
    }
    func updater(_ updater: SPUUpdater, didFinishUpdateCycleFor updateCheck: SPUUpdateCheck, error: Error?) {
        if let error {
            let value = error as NSError
            if value.domain == SUSparkleErrorDomain && value.code == SUError.noUpdateError.rawValue {
                status = "No newer compatible version is available."
            } else if value.domain == SUSparkleErrorDomain && value.code == SUError.installationCanceledError.rawValue {
                installation = nil; pending = false; status = "Update installation cancelled."
            } else { status = "Update could not finish: \(error.localizedDescription)" }
        }
        refresh()
    }
}
