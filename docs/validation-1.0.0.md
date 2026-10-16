# LookBridge 1.0.0 validation

Validated on an Apple Silicon Mac, October 15, 2026.

- The combined Python suite ran 183 tests successfully, with one case-sensitive-filesystem test skipped on this Mac's case-insensitive filesystem. It covers the original CDL, reference mapping, image preparation, navigation, PSNR, and recovery behavior, plus launcher handoff, reopen lifecycle, and support links.
- Fifteen native installer checks passed entirely in temporary directories. They cover fresh installation, menu layout, unrelated script preservation, backups of locally changed files, updates, rollback after an injected second-directory failure, receipt restoration, missing helpers, unowned folders, symlinks, malformed payloads, startup downgrade prevention, and a Resolve-running state change just before installation.
- The real Sparkle updater downloaded a signed DMG from a local fixture server, replaced a temporary application, and relaunched the replacement with a new process ID. Automatic installation waited for both window closure and Resolve closure. The replacement deferred script refresh while the simulated Resolve process was open, then updated the installation receipt after it closed.
- A tampered DMG and a tampered signed feed were both rejected; the original temporary app remained installed.
- A hidden LookBridge popup was constructed in the running DaVinci Resolve Studio 21.1.0.14 UI runtime. Expected controls and flat support links were checked. Two successive reopen sessions were verified to create fresh working windows even when Resolve retained an older hidden window. The native Mac manager was also opened and visually reviewed.

No user project, grade, source media, or installed legacy Resolve script was changed during these checks. The original core workflows were tested through their regression fixtures; this packaging work did not perform a new end-to-end grade/reference build against a real media project.

The release's installer and feed use Ed25519 verification. The app is universal arm64/x86_64 and ad-hoc signed; Apple notarization and execution on physical Intel hardware were not tested.
