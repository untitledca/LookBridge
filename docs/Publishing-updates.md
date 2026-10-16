# Publishing LookBridge updates

Stable feed: `https://github.com/untitledca/LookBridge/releases/latest/download/appcast.xml`.

LookBridge uses Sparkle 2.10.0, pinned to the official archive's SHA-256 in `tools/fetch-sparkle.sh`. Both the feed and installer require Ed25519 signatures. The updater validates archives before extraction. This follows [Sparkle's signing documentation](https://sparkle-project.org/documentation/).

## Release key

LookBridge's dedicated private key stays in the release Mac's login Keychain under account `com.atomfilmlab.lookbridge.sparkle`. It is separate from Nacho Power's key and must never be committed or included in the app or DMG.

Public key: `XcIKoHU0U6gAL3NYi9sw2CZIKOZLTdqZl8vU5KVzkIo=`.

This key is committed as `SUPublicEDKey` in `macos/Resources/Info.plist`. Preserve the private key securely for future updates. A normal source build needs only the public key; signing a release requires the private Keychain entry. Do not generate a replacement key for a distributed release.

Sparkle signatures are independent of Apple's Developer ID program. Version 1.0.0 is ad-hoc signed and not notarized; release copy must say so.

## Build and test

On a Mac with Xcode command-line tools, Python 3, and network access for the pinned Sparkle download:

```sh
python3 -m unittest discover -s tests -v
tools/test-installer.sh
tools/build.sh
tools/make-dmg.sh
tools/make-appcast.sh docs/release-notes-1.0.0.html
```

On the release Mac, run `tools/test-automatic-installer.sh` to exercise the production updater against isolated signed DMG fixtures. It requires a logged-in macOS desktop and the LookBridge signing key. It verifies replacement/relaunch, deferral while Resolve is open, subsequent script refresh, and rejection of tampered installer/feed data. It does not touch the real Resolve installation and is intentionally excluded from hosted CI.

Before each future release, increment `CFBundleShortVersionString` and the numeric `CFBundleVersion` in `macos/Resources/Info.plist`, and keep `scripts/lookbridge_support.py`'s displayed version current. Sparkle compares build versions; every published update must increase them. Read the generated artifact names from the scripts rather than renaming a signed archive afterward.

Test the Resolve launcher and both tools against an expendable project with known source/CDL/still fixtures before claiming full integration validation. Unit tests cover the existing matching, timeline mapping, comparison, and recovery logic. Constructing a popup alone does not validate grade writes or reference placement.

## Publish atomically

1. Commit and tag the tested source as `vVERSION`.
2. Create a draft release in `untitledca/LookBridge`.
3. Upload `LookBridge-VERSION-universal.dmg`, its `.sha256` file, and `appcast.xml`.
4. Download the draft assets through the authenticated API; compare their SHA-256 digests and sizes to local artifacts. Verify the exact feed bytes and DMG using `tools/verify-sparkle-update.swift` and the committed public key.
5. Publish the complete release and mark it latest. Confirm the unauthenticated latest-release API, stable appcast URL, and versioned DMG URL.

Keep immutable versioned download URLs. Do not replace a published archive with different bytes: issue a new version and regenerate signatures instead. Do not disable signature requirements to get an update to work.

## Installed files and update behavior

The Mac manager installs into `~/Applications/LookBridge.app` when launched from the DMG or another non-Applications location. The Resolve menu contains `LookBridge.py` and `Reference Still Toggle.py` in:

```text
~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility/LookBridge/
```

Private helper modules are installed outside the menu tree:

```text
~/Library/Application Support/Blackmagic Design/DaVinci Resolve/LookBridge/Modules/
```

The installer recognizes its managed directories, stages replacement files, makes backups, and rolls back failed transactions. It preserves standalone legacy scripts. Backups are under `~/Library/Application Support/LookBridge/Backups/`.

Automatic checks and automatic installation start disabled. Users can opt in. The manager must be running to check in the background. Script replacement waits until Resolve is closed, including after an app update, to avoid mixing helpers from different versions in an active workflow.
