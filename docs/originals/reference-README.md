# Resolve Reference Still Auto Overlay

`Reference Still Auto Overlay.py` builds an automatic A/B reference layer for
the current DaVinci Resolve timeline. It scans a folder of Livegrade (or other)
JPEG stills, matches each still to the original camera filename, imports the
matched files, reads each still's camera-timecode burn-in, and places a single
reference frame at that grab's mapped timeline position. It does **not** fill
the duration of the source clip with repeated stills.

The script targets DaVinci Resolve 21.0.4 and uses only the documented Resolve
scripting API for timeline and Media Pool changes.

## What it creates

- A Media Pool bin named `Reference Stills (Auto)`.
- A `Timeline Sources (Auto)` child bin containing trim-addressable helpers.
- One reference video track immediately above each enabled source video track
  containing at least one safely mapped grab.
- One one-frame, Normal-composite reference item at **100% opacity** for each
  still whose captured source timecode maps into that timeline occurrence.
- Locked tracks named `[AUTO REF] Lane 1`, `[AUTO REF] Lane 2`, and so on.
- A Pink timeline marker for each proved exact grab, or a Yellow marker when
  the mapped position is approximate. In a build containing mapped grabs, an
  unmappable occurrence receives a clearly labeled Yellow midpoint marker for
  navigation only; no image is placed at that midpoint.
- A persistent **Reference Still Review** controller for A/B and grab-to-grab
  navigation.
- A timestamped audit/build log in
  `~/Library/Logs/Resolve Reference Still Auto Overlay/`.

Placing each reference lane directly above its source preserves titles,
alternate takes, and unmatched media on higher tracks instead of covering them
with a lower layer's reference. Disabled source tracks are intentionally
skipped. Generated tracks start **disabled** so they cannot accidentally appear
in a render.

Resolve ignores a one-frame source range on an ordinary JPEG and expands it to
the user's Standard Still Duration. The script avoids that behavior by making
a private two-frame numbered image-sequence helper for each unique reference:
the second frame is a byte-identical hard link to the first. Resolve imports
the helper as video, so the script can trim one source frame and place exactly
one timeline frame. The reference is first cropped inside its detected
framelines and resized into a lossless PNG; this avoids another JPEG encoding. The
helper always remains two physical frames; source-clip duration no longer
causes hundreds or thousands of repeated timeline items.

The helpers are stored persistently in Resolve's application-support folder
under `Reference Still Auto Overlay/Sequence Cache` and appear in the
`Timeline Sources (Auto)` child bin. Keep that cache available for as long as
a project uses its generated reference tracks. Rebuilding reuses intact
helpers instead of duplicating them.

The script never changes original timeline items, grades, or source-track
locks. During visual timecode verification it briefly isolates one source
track at a time and disables subtitle tracks, then verifies and restores every
changed visibility state before continuing. It never deletes a source JPEG and
never forces a project save.

## Install

Double-click `install.command`, fully quit Resolve with `Command-Q`, and
relaunch it. The installer adds two entries to `Workspace > Scripts`:

- `Reference Still Auto Overlay` — build, rebuild, or toggle references.
- `Reference Still Toggle` — instantly toggle all generated tracks together.

The files are installed in:

```text
~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility/
```

In Resolve Preferences, scripting access must permit local scripts. The native
folder chooser, confirmation dialogs, and automatic full-frame Vision OCR
require macOS. Command-line audit and track-control operations remain available
on Windows and Linux, but this sparse builder will not place an image unless a
grab timecode can be mapped safely.

## Use

1. Open the project and the timeline containing the camera originals.
2. Run `Workspace > Scripts > Reference Still Auto Overlay`.
3. Choose the folder containing the Livegrade JPEGs.
4. Review the audit summary and click **Build**.
5. Choose **Normal** or **Difference** from the comparison dropdown.
   Use **Reference ON**, **Original / OFF**, **Previous Grab**, and **Next Grab**
   in the review controller. **Toggle A/B** remains available in the
   controller, and `Workspace > Scripts > Reference Still Toggle` remains the
   fastest one-click menu or keyboard-shortcut toggle when the controller is
   closed.

Running `Reference Still Auto Overlay` again opens the controller immediately.
Its **Rebuild…** button lets you choose a different JPEG folder without closing
the review workflow. The controller follows the currently open timeline and
reads the playhead fresh on every Previous/Next click, so manual scrubbing does
not make its navigation index stale. Left/Right Arrow and `[`/`]` also step
between grabs while the controller has focus.

References are intentionally only one frame long. Stop on a Pink or Yellow
review marker, or use **Previous Grab**/**Next Grab**, before switching the
reference on. At a mapped grab, **Normal** starts at 100% opacity: **Reference ON**
shows the reference, and **Original / OFF** shows the camera frame. The opacity
slider allows 0–100%; it is not fixed at 50%. **Toggle A/B** switches between those two
states. At an unmappable Yellow midpoint marker the controller reports
**MARKER ONLY — NO IMAGE PLACED**, so enabling the overlay there is expected
to leave the viewer unchanged.

## Comparison and automatic cropping

**Rebuild existing references once after installing this version** to replace
uncropped JPEG helpers with frameline-cropped PNG helpers. Opening the controller
starts in Normal at 100%; mode and opacity are retained while navigating grabs.

- **Normal:** full-opacity reference/original A/B by default.
- **Difference:** Resolve's Difference composite. At 100% opacity, equal output
  pixels produce black. The toggle switches between Difference and the original.
- **Measure PSNR — current grab:** a separate measurement button, not a composite
  mode. It runs only when clicked, never automatically on a toggle, opacity edit,
  mode change, or Previous/Next. The result says **Last measured**; click again
  after a grade change. Both camera and
  reference are exported separately through Resolve at the same frame; reference
  measurement temporarily uses Normal/100%, independent of the opacity slider.
  Track visibility, reference properties/lock, page and playhead are restored.
  The palette briefly hides during capture and returns afterward. If measurement
  is unavailable, it explains why and leaves the selected A/B mode unchanged;
  choose Difference yourself for a visual check. A restoration failure is shown
  as critical, with no follow-up comparison changes.

The score includes RGB8 RMS error as a percentage of full-scale code value:
`100 × sqrt(MSE) / 255`. Higher PSNR means lower numeric error. As a conversion
guide, 40 dB is about 1% RMS, 34 dB about 2%, and 30 dB about 3.2%. Below 30 dB
the guide recommends checking frame, framing, and color; 30–40 dB recommends
visual inspection; 40+ dB indicates low numeric pixel error, still requiring
inspection. These are working numeric-error bands, **not** certified perceptual
quality thresholds or automatic grade pass/fail. Two percent RMS does not mean
two percent of pixels changed, or that a difference is visually imperceptible.
The formulas follow the [MathWorks PSNR definition](https://www.mathworks.com/help/images/ref/psnr.html).

The image detector searches for a supported frameline rectangle across the full
capture, rather than assuming Sony/ARRI-specific coordinates or timecode corners.
It extracts the pixels just inside the line, excluding the line and outer monitor
UI. The extracted image is fit, without stretching, onto the timeline-resolution
canvas, with black padding when aspect ratios differ. The untouched JPEG is still
used for filename matching and OCR. Cropping never overwrites the original.
Ambiguous crops are skipped and reported; if none are safe the old tracks remain.
Detected crop rectangles and prepared-file checksums are recorded in cache JSON
sidecars and the build report. Clean, unframed images may use their full frame.

Cropping cannot infer a creative camera-original crop, undo an incorrect de-squeeze,
remove a crosshair burned into the picture, or fix mismatched display transforms.
The camera timeline must use the same intended framing, de-squeeze, LUT/output
transform and data levels as the on-set image. A frameline is treated as the intended
image boundary, so check it if your camera uses action-safe lines instead.

PSNR is a full-canvas, equally weighted RGB8 output-pixel measurement:
`10 × log10(255² / MSE)`. Identical pixels report infinite PSNR. JPEG compression,
resampling, differing frames, black padding, monitor graphics, scaling and color
management all affect it; a high score alone does not prove a CDL was applied
correctly. This is not a native Resolve analysis mode or a linear-light/HDR metric.
An approximate timecode marker is explicitly flagged in the score. References
containing embedded display LUTs must not receive that look twice in Resolve.

The supplied Sony test MOVs contain monitor graphics themselves. Cropping only
the reference correctly prepares it for an OCN comparison, but it will not align
with those uncropped monitor-recording MOVs. Use matching camera-original media
with equivalent output framing to evaluate the cropped A/B and PSNR.

Resolve 21.0.4 does not expose playhead timecode control on every page. If the
controller is opened or used from Media, Photo, or Fusion, it moves Resolve to
the Edit page so Previous/Next navigation remains reliable.

Do not use Resolve's **Image Wipe** button for this workflow. Image Wipe
requires a selected Gallery still; the script deliberately creates timeline
overlays so the references can follow the playhead and be controlled through
the supported scripting API.

Running **Rebuild…** stages and verifies the new tracks, temporarily moves
the prior working tracks into a disabled backup namespace, and only then
removes the backups. If staging or final validation fails, the prior working
reference tracks are restored. If Resolve refuses only the final cleanup, the
new references remain usable; the script contains the survivor as a locked,
disabled recovery track and shows a cleanup warning. If Resolve also refuses
that containment, the warning names the exact manual disable/lock action still
needed.

Rebuild after changing source-track enable states or materially rearranging
the edit. The reference layout represents the enabled tracks and clip ranges
that existed at build time.

You can also toggle the generated tracks with Resolve's video-track enable
controls. Do not rename or add media to `[AUTO REF]` tracks. The script locks
them and refuses cleanup if a managed track is unlocked or contains unknown
media.

## Filename matching

Matching is exact and case-insensitive. The final `.jpg` or `.jpeg` extension
is removed from the reference filename and compared with the source basename.
The physical source path is preferred over a renamed Media Pool or timeline
clip name.

| Timeline source | Accepted reference filename |
| --- | --- |
| `A001_C001_0101AB.mov` | `A001_C001_0101AB.jpg` |
| `A001_C001_0101AB.mov` | `A001_C001_0101AB.mov.jpg` |
| `A001_C001_0101AB.ari` | `A001_C001_0101AB.jpeg` |
| `A001_C001_0101AB_001.R3D` | `A001_C001_0101AB_001.jpg` |
| `A001_C001_0101AB_001.R3D` | `A001_C001_0101AB.jpg` |
| `C0007.MXF` | `C0007.JPG` |

For RED media, the physical chunk name is tried before the logical clip name
with its final `_001`/`_0001` suffix removed. `.RDC` parent-folder names and
Resolve image-sequence display names are also supported. If two JPEGs in the
selected folder have the same match key, that key is ambiguous and is skipped;
the script never guesses based on date or folder order. Physical path identity
respects case-sensitive filesystems even though filename matching itself is
case-insensitive.

## Exact grab markers

The supplied Livegrade JPEGs do not contain an EXIF/XMP timecode or useful
sidecar metadata. On macOS, the script therefore runs Apple's built-in Vision
text recognition across the **entire image**, finds timecode-shaped text, and
accepts a candidate only when it falls inside Resolve's timecode and frame
range for the filename-matched source. It does not assume that timecode is in a
Sony-specific corner or use a fixed crop, so a readable ARRI, RED, Sony, or
custom-monitor burn-in may place the timecode anywhere in the image.

The Vision bridge explicitly decodes OCR output as UTF-8 and replaces malformed
bytes instead of aborting. Reports are also written as UTF-8, so non-ASCII
filenames or recognized text do not depend on the shell's default locale.

For each metadata-derived exact position, the script also asks Resolve to
export that full-resolution timeline frame with only the intended source layer
visible, with subtitle tracks disabled, and checks its burn-in across the
entire image. The exported TC must appear in the same normalized image region
as the TC found in the reference, which prevents a subtitle, slate TC, or
Resolve Data Burn-In elsewhere in the frame from being mistaken for camera TC.
A unique discrepancy of at most two frames is corrected, then the corrected
frame is exported and verified again. This catches recorder metadata that is
one frame ahead of or behind the visible camera timecode. Boundary candidates
within that same two-frame limit are tentatively clamped to the occurrence edge
and accepted only if this visual proof succeeds. Every temporarily changed
track state and the original playhead position are restored, and these
verification exports are deleted afterward.

For a normal-speed occurrence containing the captured source frame, the script
places its one-frame reference and adds a Pink exact marker; **Next Grab** lands
on that frame. A readable TC that can be mapped but not independently proved
at pixel level remains placeable, but receives a Yellow approximate marker and
an explanation. Examples include failed visual verification, an exposed
non-100%/duration-changing retime, or a timecode that Vision split across
observations so its image region cannot be proved safely. Source media without
a readable TC in Resolve's exported camera frame can still be mapped from its
embedded source range, but is conservatively labeled approximate because the
independent pixel-level check is unavailable.

If the still has no readable camera TC, more than one in-range TC is plausible,
the TC falls outside that edit occurrence, or Resolve exposes no usable source
range, the script does not place an image at a guessed frame. In a mixed build,
it adds a Yellow marker at that occurrence's midpoint strictly as a navigation
aid; the marker and controller both say that no image was placed. If **none**
of the matched references can be mapped, the build stops before changing the
timeline and leaves the prior working overlay intact.

Resolve does not expose every variable-speed curve through this scripting API,
so review a speed-ramped clip manually even if its endpoints happen to have a
1:1 duration.
A user marker already occupying a target frame is never overwritten or
shifted; the controller can still use that review point during the current
build session and the report records the collision.

Only markers whose hidden custom data begins with the script's ownership
namespace are updated or removed. Marker color or a similar-looking name is
never treated as proof of ownership.

Transitions, variable retimes, split screens, and other timeline composites
are not recreated in the still lane, so compare away from those effects.

A JPEG placed on a Resolve timeline passes through Resolve's sizing,
color-management, timeline-grade, and output pipeline. For a visually matched
comparison, the Livegrade export and Resolve monitoring path must agree on
resolution/aspect ratio, display color space, gamma, data levels, output
transform, and any blanking or burn-ins. The script deliberately does not
guess or rewrite color-space/data-level metadata.

JPEG compression also prevents a mathematically perfect pixel comparison.
The optional Difference mode is useful for spotting meaningful mismatches, but
an otherwise correct JPEG comparison may not be completely black.

Resolve's public scripting API can import Gallery stills, but it cannot select
or play a Gallery still or toggle the native Gallery wipe. Timeline overlay
tracks are used because they can follow the playhead and be toggled entirely
through supported APIs.

## Remove generated tracks

From a terminal while Resolve is running:

```bash
python3 "Reference Still Auto Overlay.py" --remove
```

The command recognizes only the exact reserved final, interrupted-build, and
backup track namespaces. Final and backup tracks must be locked and contain
only script-labeled JPEGs; an interrupted staging track may be empty. The
script refuses cleanup when it cannot prove a track belongs to it. Imported
JPEGs remain in the `Reference Stills (Auto)` Media Pool bin because Resolve
cannot reliably prove that a Media Pool item is unused elsewhere. Source files
remain untouched.

## Command-line controls

Resolve must be running with the target project and timeline open.

```bash
python3 "Reference Still Auto Overlay.py" --folder /path/to/stills --audit-only
python3 "Reference Still Auto Overlay.py" --folder /path/to/stills --build --yes
python3 "Reference Still Auto Overlay.py" --toggle
python3 "Reference Still Auto Overlay.py" --show-reference
python3 "Reference Still Auto Overlay.py" --show-camera
python3 "Reference Still Auto Overlay.py" --difference
python3 "Reference Still Auto Overlay.py" --remove --yes
```

`--non-recursive` limits the JPEG scan to the selected folder. By default,
hidden files/directories are ignored and visible subfolders are scanned.

The companion `CDL Auto Apply.py` in this workspace has also been updated to
exclude every `[AUTO REF…]` track, so applying CDLs after building references
does not grade the imported JPEGs a second time.

## Verification expectations

### Local validation status — September 10, 2026

The local application reports Resolve 21.1.0.14 (not 21.0.4). The live test
timeline rebuilt successfully with 20 cropped, one-frame references and 20
exact markers. Source layout and clip properties were verified unchanged.
After Resolve was relaunched, the standalone **Measure PSNR** button passed
live UI tests from both Normal/100%/OFF and Difference/42%/ON. The same frame
returned the same 14.78 dB score in both cases, independently of the display
mode/opacity. Snapshots of all video/subtitle track states, clip properties,
locks, page and playhead matched exactly before and after each measurement.
The palette returned with the original viewing controls and its score guide.
This replaces the earlier automatic-PSNR dropdown path, whose final test had
stalled the scripting bridge. The updated automated suite ran 144 tests with
one platform-specific skip; the 26 CDL-script tests also passed. Resolve
21.0.4 itself was not available for direct runtime testing.

The included unit tests cover filename matching and audit safety, the reusable
two-frame helper, one sparse one-frame placement per mapped grab, verified 100%
default opacity, Normal/Difference transactions and slider ranges, frameline
cropping/PNG cache integrity, PSNR math and capture-state restoration, full-frame OCR and UTF-8 decoding, bounded
burn-in correction, marker-only midpoint fallbacks, script-owned marker
safety, controller navigation, transactional rebuild/rollback, and group
toggle behavior.

After a successful Resolve build, the practical checks are:

- Each safely mapped grab produces exactly one `[AUTO REF]` item whose timeline
  start-to-end duration is one frame, regardless of the source clip's length.
- Every generated item starts in Normal composite mode at 100% opacity, and all
  managed tracks start locked and disabled.
- Pink markers identify proved exact frames. Yellow markers explain either a
  mapped-but-approximate frame or a marker-only midpoint with no overlay item.
- **Previous Grab** and **Next Grab** seek to those review stops; **Reference
  ON**, **Original / OFF**, and **Toggle A/B** work without closing the controller.
- Rebuild replaces only proved managed tracks, keeps the prior build if staging
  or validation fails, and never saves the project automatically.

A timeline containing hundreds of `[AUTO REF]` items across whole source-clip
durations is from the obsolete full-duration design. Rebuild it with this
version; the new item count follows mapped grabs, not source duration.
