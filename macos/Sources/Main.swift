import AppKit
import SwiftUI

@main
struct LookBridgeMain {
    @MainActor static func main() {
        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.delegate = delegate
        app.setActivationPolicy(.regular)
        withExtendedLifetime(delegate) { app.run() }
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    private var window: NSWindow!
    private let model = AppModel()
    private let updater = AppUpdater()
    private var pendingURLCheck = false
    func applicationDidFinishLaunching(_ notification: Notification) {
        makeMenus()
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 780, height: 680), styleMask: [.titled, .closable, .miniaturizable], backing: .buffered, defer: false)
        window.title = "LookBridge"
        window.titlebarAppearsTransparent = true
        window.titleVisibility = .hidden
        window.backgroundColor = NSColor(calibratedRed: 0.055, green: 0.066, blue: 0.08, alpha: 1)
        window.appearance = NSAppearance(named: .darkAqua)
        window.isReleasedWhenClosed = false
        window.delegate = self
        window.contentView = NSHostingView(rootView: MainView(model: model, updater: updater))
        window.center()
        updater.mayInstallAutomatically = { [weak self] in
            guard let self else { return false }
            return !self.model.busy && !self.model.resolveRunning && self.window?.isVisible != true
        }
        model.onStateRefresh = { [weak self] in self?.updater.reevaluate() }
        showWindow()
        updater.start()
        if pendingURLCheck { pendingURLCheck = false; updater.check() }
    }
    func application(_ application: NSApplication, open urls: [URL]) {
        guard urls.contains(where: { $0.scheme == "lookbridge" && $0.host == "check-for-updates" }) else { return }
        guard window != nil else { pendingURLCheck = true; return }
        showWindow()
        DispatchQueue.main.async { self.updater.check() }
    }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool { showWindow(); return true }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
    func windowWillClose(_ notification: Notification) { DispatchQueue.main.async { self.updater.reevaluate() } }
    private func showWindow() { window?.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true) }
    @objc private func checkUpdates() { showWindow(); updater.check() }
    @objc private func showAbout() { NSApp.orderFrontStandardAboutPanel(nil) }
    @objc private func showReleasePage() { NSWorkspace.shared.open(URL(string: "https://github.com/untitledca/LookBridge/releases")!) }
    private func makeMenus() {
        let menu = NSMenu()
        let appItem = NSMenuItem()
        let appMenu = NSMenu(title: "LookBridge")
        appMenu.addItem(withTitle: "About LookBridge", action: #selector(showAbout), keyEquivalent: "").target = self
        appMenu.addItem(NSMenuItem.separator())
        appMenu.addItem(withTitle: "Check for Updates…", action: #selector(checkUpdates), keyEquivalent: "u").target = self
        appMenu.addItem(NSMenuItem.separator())
        appMenu.addItem(withTitle: "Hide LookBridge", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(withTitle: "Quit LookBridge", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu; menu.addItem(appItem)
        let help = NSMenuItem(); let helpMenu = NSMenu(title: "Help")
        helpMenu.addItem(withTitle: "LookBridge on GitHub", action: #selector(showReleasePage), keyEquivalent: "").target = self
        help.submenu = helpMenu; menu.addItem(help)
        NSApp.mainMenu = menu
    }
}

private struct MainView: View {
    @ObservedObject var model: AppModel
    @ObservedObject var updater: AppUpdater
    private let accent = Color(red: 0.45, green: 0.89, blue: 0.76)
    private let muted = Color(red: 0.61, green: 0.66, blue: 0.70)

    var body: some View {
        VStack(alignment: .leading, spacing: 22) {
            HStack(spacing: 18) {
                Image(nsImage: NSImage(named: NSImage.applicationIconName) ?? NSImage()).resizable().frame(width: 84, height: 84)
                VStack(alignment: .leading, spacing: 6) {
                    Text("LOOKBRIDGE").font(.system(size: 12, weight: .semibold, design: .monospaced)).tracking(3).foregroundColor(accent)
                    Text("Bring your look across.").font(.system(size: 30, weight: .semibold))
                    Text("CDLs and reference stills, connected to your timeline.").font(.system(size: 13)).foregroundColor(muted)
                }
                Spacer()
            }
            HStack(spacing: 12) {
                toolCard(icon: "slider.horizontal.3", title: "CDL Auto Apply", detail: "Match CDL files to clips and apply your color decisions in Resolve.")
                toolCard(icon: "rectangle.on.rectangle", title: "Reference Still Overlay", detail: "Pair reference stills with clips and compare the image in your timeline.")
            }
            VStack(alignment: .leading, spacing: 14) {
                HStack {
                    Label(model.installed ? "Installed · v\(model.version)" : "One install. Both tools.", systemImage: model.installed ? "checkmark.circle.fill" : "arrow.down.circle")
                        .font(.system(size: 16, weight: .semibold)).foregroundColor(model.installed ? accent : .white)
                    Spacer()
                    Text("FOR DAVINCI RESOLVE STUDIO").font(.system(size: 10, weight: .medium, design: .monospaced)).foregroundColor(muted)
                }
                Text(model.status).font(.system(size: 13)).foregroundColor(model.failed ? Color(red: 1, green: 0.6, blue: 0.58) : muted).fixedSize(horizontal: false, vertical: true).frame(minHeight: 35, alignment: .topLeading)
                HStack(spacing: 14) {
                    Button(action: model.install) {
                        Text(model.busy ? "Installing…" : model.installed ? "Reinstall Resolve Tools" : "Install LookBridge")
                            .font(.system(size: 14, weight: .semibold)).padding(.horizontal, 19).padding(.vertical, 11)
                    }.buttonStyle(.plain).background(accent).foregroundColor(Color(red: 0.04, green: 0.11, blue: 0.10)).cornerRadius(9).disabled(model.busy)
                    if model.installed {
                        Button("Open Resolve", action: model.openResolve).buttonStyle(.link)
                        Button("Show scripts", action: model.showScripts).buttonStyle(.link)
                    } else {
                        Text("No administrator password needed.").font(.system(size: 11)).foregroundColor(muted)
                    }
                    Spacer()
                }
                if model.resolveRunning { Label("Resolve is open. Quit it before installing or refreshing scripts.", systemImage: "info.circle").font(.system(size: 11)).foregroundColor(muted) }
            }.padding(20).background(Color.white.opacity(0.055)).cornerRadius(14)
            VStack(alignment: .leading, spacing: 11) {
                HStack {
                    Text("App updates").font(.system(size: 15, weight: .semibold))
                    Spacer()
                    if updater.pending { Button("Install & Relaunch", action: updater.installPending).buttonStyle(.link) }
                    Button("Check for Updates…", action: updater.check).buttonStyle(.link).disabled(!updater.canCheck || model.busy)
                }
                Toggle("Check automatically for updates", isOn: Binding(get: { updater.checks }, set: updater.setChecks)).toggleStyle(.checkbox)
                Toggle("Download and install updates automatically", isOn: Binding(get: { updater.automaticInstallation }, set: updater.setAutomaticInstallation)).toggleStyle(.checkbox)
                Text(updater.status).font(.system(size: 11)).foregroundColor(muted).lineLimit(2)
                Text("Automatic installation waits until this window is closed and Resolve is quit. Updated scripts are refreshed on relaunch.").font(.system(size: 11)).foregroundColor(muted)
            }.font(.system(size: 12))
            Spacer(minLength: 0)
            HStack {
                Text("ATOM FILM LAB").font(.system(size: 10, weight: .medium, design: .monospaced)).tracking(1.5).foregroundColor(muted)
                Spacer()
                Text("Free to use.").font(.system(size: 11)).foregroundColor(muted)
                Menu("Support development ↗") {
                    ForEach(AppSupportLinks.options) { option in Button("\(option.label) · QuickBooks") { NSWorkspace.shared.open(option.checkoutURL) } }
                }.menuStyle(.borderlessButton).fixedSize().font(.system(size: 11)).foregroundColor(accent)
                Text("v\(model.version)").font(.system(size: 10, design: .monospaced)).foregroundColor(muted)
            }
        }.padding(.horizontal, 32).padding(.top, 22).padding(.bottom, 24)
        .frame(width: 780, height: 680)
        .background(Color(red: 0.055, green: 0.066, blue: 0.08)).preferredColorScheme(.dark)
    }
    private func toolCard(icon: String, title: String, detail: String) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 9) {
                Image(systemName: icon).font(.system(size: 17)).foregroundColor(accent)
                Text(title).font(.system(size: 14, weight: .medium))
            }
            Text(detail).font(.system(size: 12)).foregroundColor(muted).fixedSize(horizontal: false, vertical: true)
        }.frame(maxWidth: .infinity, minHeight: 73, alignment: .topLeading).padding(18)
        .background(Color.white.opacity(0.035)).overlay(RoundedRectangle(cornerRadius: 13).stroke(Color.white.opacity(0.08), lineWidth: 1)).cornerRadius(13)
    }
}
