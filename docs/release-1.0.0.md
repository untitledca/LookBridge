LookBridge combines **CDL Auto Apply** and **Reference Still Auto Overlay** in one DaVinci Resolve Studio launcher.

- Match ASC CDLs to source filenames, audit, then apply to dedicated CDL nodes.
- Align reference stills to camera timecode and review with A/B, Difference, opacity, grab navigation, and PSNR.
- Install both workflows with one universal Mac app.
- Check for updates manually or opt into automatic checking and installation.
- Optional $5 / $10 / $25 QuickBooks support links in the Resolve window. All features remain free.

### Install

1. Download **LookBridge-1.0.0-universal.dmg** below and open it.
2. Quit DaVinci Resolve, open **LookBridge**, and click **Install LookBridge**.
3. Open Resolve Studio and choose **Workspace → Scripts → LookBridge → LookBridge**.

Requires **macOS 12+**, **Apple Silicon or Intel**, and **DaVinci Resolve Studio** with scripting support. Resolve's own macOS requirements still apply.

This release is **not Apple-notarized**. If macOS blocks first launch, use **System Settings → Privacy & Security → Open Anyway**. The update feed and DMG are independently verified with LookBridge's dedicated Sparkle Ed25519 key.

Existing standalone scripts are preserved. Automatic updates start disabled; new script files wait until Resolve is closed before installation. Project changes still require the original tools' audit and confirmation, and projects are not force-saved.

The script regression tests and native installer/update checks are described in the repository. The LookBridge popup was checked in Resolve Studio 21.1.0.14; this packaging release does not claim a new full grade/reference integration run on a real project.

[Source and instructions](https://github.com/untitledca/LookBridge) · [Atom Film Lab Apps](https://www.atomfilmlab.com/apps)
