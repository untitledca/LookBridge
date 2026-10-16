# Resolve CDL Auto Apply

`CDL Auto Apply.py` scans a folder for ASC CDL files, matches each CDL to the
source filename of every video clip on the current timeline, and writes the CDL
to a dedicated clip-level node labeled `CDL`.

It is deliberately conservative:

- Existing nodes are never reset, replaced, copied, or reordered.
- An existing node labeled `CDL` is updated in place.
- A missing `CDL` node is appended at the end of the Clip Layer 1 graph.
- The current active local/remote color version for each clip is targeted.
- Duplicate CDL filenames and duplicate `CDL` node labels are skipped.
- The script audits first and asks for confirmation before changing grades.
- Track locks are not changed. Temporary track visibility changes used to
  select obscured clips are restored immediately.
- Resolve is not told to save the project after the run.

## Requirements

- macOS and DaVinci Resolve 18 or newer, including Resolve 21.
- Resolve's interface language must be English for automatic node creation.
- In Resolve Preferences, scripting access must permit local scripts.
- Automatic node creation needs macOS Accessibility/Automation permission the
  first time Resolve asks for it.

The official Resolve scripting API can set CDL values on a numbered node, but
it cannot create or rename color nodes. The macOS helper only invokes Resolve's
own `Color > Nodes > Append Node` and `Label Selected Node` commands. The script
then verifies the new node in the clip graph before calling `SetCDL`.

## Install

Double-click `install.command`, fully quit Resolve with `Command-Q`, and
relaunch it. The installer places the script in the always-visible Utility
folder:

```text
~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility/CDL Auto Apply.py
```

The script is also already installed at that location when delivered on this
machine. It appears directly in `Workspace > Scripts`; there is no `Color`
submenu. During installation, older active copies named `CDL Auto Apply.py` or
`CDL_Auto_Apply.py` are retired from both the user and system Resolve script
folders, so only the new Utility copy remains active. Retired copies are kept
with a non-script `.retired-<timestamp>` suffix for recovery.

## Run

1. Save a project backup before any batch color operation.
2. Open the locked timeline and go to the Color page.
3. Set the node graph selector to `Clip - L1`.
4. Choose `Workspace > Scripts > CDL Auto Apply`.
5. Select the CDL folder, review the audit, then click `Apply`.

The script scans subfolders and supports `.cdl`, `.cc`, and `.ccc` XML files.
For a `.ccc` containing several corrections, one correction ID must uniquely
match the CDL filename.

## Filename Matching

Matching is exact and case-insensitive, and does not depend on a camera or codec
list. The script takes the source basename, removes its final file extension,
and compares that result with the CDL basename after removing `.cdl`, `.cc`, or
`.ccc`. A CDL may also include the original media extension.

| Timeline source | Accepted CDL filename |
| --- | --- |
| `A001_C001_0101AB.mov` | `A001_C001_0101AB.cdl` |
| `A001_C001_0101AB.mov` | `A001_C001_0101AB.mov.cdl` |
| `A001_C001_0101AB.ari` | `A001_C001_0101AB.cdl` |
| `A001_C001_0101AB_001.R3D` | `A001_C001_0101AB_001.cdl` |
| `A001_C001_0101AB_001.R3D` | `A001_C001_0101AB.cdl` |
| `DJI_20260315_001.MP4` | `DJI_20260315_001.cdl` |
| `CAM_A_001_C0001.BRAW` | `CAM_A_001_C0001.cdl` |
| `C0007.MXF` | `C0007.cc` |
| `SHOT_0042.NEWRAW` | `SHOT_0042.cdl` |

The source file path is preferred. Media Pool and timeline clip names are used
as fallbacks when Resolve does not expose a source path. For spanned RED media,
the exact numbered `.R3D` filename is tried first, followed by the logical RED
clip name with the final chunk number removed.

## Reports And Recovery

Every audit and apply run writes a detailed log to:

```text
~/Library/Logs/Resolve CDL Auto Apply/
```

If the Color page is not showing `Clip - L1`, a newly appended node is deleted
again, further automatic node creation stops, and the clip is reported as
failed. If macOS blocks Accessibility, no missing nodes are created; clips that
already have a valid `CDL` node can still be updated.

The script does not force-save the project. Review several clips and the report,
then save normally when the result is correct.

## Command Line Options

Resolve must be running with the target project and timeline open.

```bash
python3 "CDL Auto Apply.py" --folder /path/to/cdls --audit-only
python3 "CDL Auto Apply.py" --folder /path/to/cdls --apply --yes
python3 "CDL Auto Apply.py" --folder /path/to/cdls --apply --yes --no-create-nodes
```

`--no-create-nodes` is useful on non-macOS systems or in facilities that
pre-build a labeled `CDL` node in their standard node template.
