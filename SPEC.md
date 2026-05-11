# pic-to-patch: Embroidered Mission Patch Generator

## What this does

Takes any image and outputs a photorealistic embroidered mission patch PNG.
Uses real embroidery software (inkstitch) to compute actual stitch paths,
then post-processes the stitch simulation for photorealism. No AI/GPU required.

## The pipeline

```
input image (.png/.jpg/.svg)
        │
        ▼
┌─────────────────┐
│  1. RESIZE      │  Scale to max 500px (preserves aspect ratio)
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  2. VECTORIZE   │  vtracer: raster → color SVG (separated regions)
└────────┬────────┘  Produces 30-1200+ paths depending on complexity.
         │           SVG inputs skip steps 1-2.
         ▼
┌─────────────────┐
│  3. PARAMETERIZE│  Add inkstitch embroidery attributes to every path:
└────────┬────────┘  fill_method, angle, row_spacing, underlay, staggers.
         │           Add border rect. Set mm dimensions. Add version metadata.
         ▼
┌─────────────────┐
│  4. STITCH SIM  │  inkstitch --extension=png_realistic
└────────┬────────┘  Computes real stitch paths and renders thread simulation.
         │           This is the core — it's what makes it look embroidered.
         ▼
┌─────────────────┐
│  5. POSTPROCESS │  13-stage photorealistic pipeline:
└─────────────────┘  normal maps, Blinn-Phong shading, thread grain,
                     fabric texture, drop shadow, merrow edge, vignette.
                     Output: final PNG on dark fabric background.
```

## Two entry points

### `pic2patch.py` — raster images
```bash
python3 pic2patch.py input.png [output.png] [-b "#0a0a14"] [--color-precision 8]
```
Full pipeline: resize → vtracer → parameterize → inkstitch → postprocess.

### `svg2patch.py` — SVG inputs  
```bash
python3 svg2patch.py input.svg [output.png] [-b "#0a0a14"] [--no-border]
```
Steps 3-5 only. Works with any SVG: vtracer output, hand-crafted designs,
Figma exports, etc. Just needs filled paths (no strokes-only).

## Dependencies

### Required binaries
- **vtracer** (`~/.cargo/bin/vtracer`) — color-aware raster-to-SVG vectorizer
  Install: `cargo install vtracer`
- **inkstitch** — embroidery design engine, runs headless
  Install: download from https://inkstitch.org, extract to:
  `~/Library/Application Support/org.inkscape.Inkscape/config/inkscape/extensions/inkstitch.app/`
  Binary at: `.../inkstitch.app/Contents/MacOS/inkstitch`

### Python packages
- `lxml` — SVG/XML manipulation
- `numpy` — array ops for post-processing
- `scipy` — ndimage (gaussian_filter, binary morphology) for post-processing
- `Pillow` — image I/O and filters

### NOT required
- Inkscape (inkstitch binary runs standalone)
- GPU / CUDA
- Any AI model or API
- ComfyUI (explored, not in main pipeline)

## File structure (current → proposed)

```
Current (in attempts/inkstitched/):        Proposed (top-level):
├── pic2patch.py                           ├── bin/
├── svg2patch.py                           │   └── generate          # entry point
├── postprocess.py          (unused)       ├── lib/
├── normalmap/                             │   ├── pic2patch.py      # raster pipeline
│   └── photorealistic.py                  │   ├── svg2patch.py      # SVG pipeline
├── comfyui/                (experimental) │   └── photorealistic.py  # post-processing
├── blender/                (experimental) ├── test-inputs/           # test images
├── batch/                  (test data)    ├── tmp/                   # outputs
└── nasa_patch.svg          (reference)    ├── requirements.txt
                                           └── SPEC.md
```

## What works well

- **Color fidelity**: vtracer preserves 30-60+ colors from source images.
  Old potrace approach collapsed everything to 2 colors.
- **Stitch realism**: inkstitch computes real stitch paths with proper
  fill angles, underlay, stagger patterns. Not a filter — actual embroidery math.
- **Post-processing**: the 13-stage normal map pipeline adds convincing depth,
  thread texture, fabric backing, and edge effects.
- **Borders**: rounded rect border behind content, parameterized color.
- **SVG passthrough**: hand-crafted SVGs (like the NASA meatball) render
  beautifully. This is the "gold path."
- **Speed**: ~10-30 seconds per image on an M-series Mac. No GPU needed.

## What needs work

### Must-fix before shipping
1. **Border shape**: currently always a rounded rectangle matching the bounding
   box. For non-rectangular content (like the raccoon), should either:
   - Dilate the content silhouette to create a contour-following border, OR
   - Let the user choose between "rect", "circle", "contour" border shapes
2. **Cleanup temp files**: vtracer SVG and resized PNG should be cleaned up
   (or kept behind a --debug flag).
3. **Error handling**: if vtracer or inkstitch aren't installed, fail with
   a helpful message instead of a traceback.
4. **Wire into bin/generate**: the existing Ruby wrapper calls `PatchGenerator`
   (ImageMagick crosshatch). Should call the new Python pipeline instead.

### Nice-to-have
5. **Transparent background option**: output patch with alpha instead of
   fabric backing (for compositing into other designs).
6. **Configurable patch size**: currently hardcoded at 80mm width.
7. **Color precision control**: vtracer's `--color_precision` (1-8) works but
   we should expose it more prominently. Lower = fewer colors = faster render.
8. **Batch mode**: process a directory of images in one command.
9. **ComfyUI integration**: optional GPU-accelerated realism pass using
   FLUX tile ControlNet. Explored, works well (strength 0.78), but adds
   heavy dependencies. Keep as opt-in upgrade path.

## Key technical details for implementers

### inkstitch SVG requirements
- Namespace: `xmlns:inkstitch="http://inkstitch.org/namespace"`
- Version metadata: `<metadata><inkstitch:inkstitch_svg_version>3</inkstitch:inkstitch_svg_version></metadata>`
- Document units: `<sodipodi:namedview inkscape:document-units="mm"/>`
- Dimensions must be in mm (e.g., `width="80mm"`)
- Each filled path needs: `inkstitch:fill_method="auto_fill"`,
  `inkstitch:angle`, `inkstitch:row_spacing_mm`, `inkstitch:max_stitch_length_mm`,
  `inkstitch:staggers`, optionally `inkstitch:fill_underlay="true"`

### Fill angle strategy
- Vary angles across elements to avoid moiré and add visual interest
- Current formula: `(30 + element_index * 23) % 180`
- Underlay angle is always perpendicular to fill angle

### Post-processing pipeline stages
1. Mask extraction (alpha from stitch render)
2. Normal map computation (Sobel gradients)
3. Thread direction estimation (from stitch angles)
4. Blinn-Phong + Kajiya-Kay anisotropic shading
5. Ambient occlusion
6. Per-thread micro-highlights
7. Fabric texture generation (weave pattern + noise)
8. Drop shadow
9. Composite patch onto fabric
10. Edge bevel (3D thickness)
11. Inner relief (section boundary depth)
12. Merrow edge border simulation
13. Photographic finishing (DOF, color grading, vignette, grain)

## Known issues and gotchas

### 1. inkstitch output format and stdout behavior
`inkstitch --extension=png_realistic <input.svg>` writes PNG data to **stdout**.
It does not create any side-effect files alongside the input SVG. Verified by
running it and checking: no files created, stdout is valid RGBA PNG data.

The `--output` flag does NOT work — it's silently ignored (stdout still gets the
PNG). The `--help` output only shows `--extension` as a flag. This behavior was
discovered empirically, not from documentation. inkstitch's CLI is minimal and
undocumented beyond `--extension`.

**Defensive approach**: if a future version changes this, the symptom would be
a 0-byte or corrupt output file. The current code already checks
`output_path.exists() and output_path.stat().st_size > 0`.

### 2. inkstitch version compatibility
We're running **v3.2.2** (from `Info.plist` `CFBundleShortVersionString`). This is
"minimum known working" only because it's the only version tested — no actual
version-specific bugs were encountered. We didn't test older versions.

The SVG version metadata `<inkstitch:inkstitch_svg_version>3` corresponds to
ink/stitch v3.x. Without this metadata element, inkstitch pops up a GUI dialog
asking to update the SVG, which blocks headless operation. This was the only
version-related bug we hit.

v3.3+ should work fine — the SVG attributes we use (`fill_method`, `angle`,
`row_spacing_mm`, etc.) are stable and have been in inkstitch for years. The only
thing to watch for: if they bump the SVG version requirement to `4`, the version
metadata would need updating.

### 3. White background / white content mask conflict
The post-processor (`photorealistic.py`) identifies the patch region by treating
all pixels with RGB > 235 as background. inkstitch renders on an **opaque white
background** (alpha=255 everywhere, background is RGB 255,255,255). There is no
way to configure inkstitch's background color for `png_realistic` — the CLI has
no flags for it.

**Known problem**: white embroidered thread (like the NASA text) produces pixels
ranging from ~221-255 RGB. The threshold at 235 misclassifies some white stitch
pixels as background, causing mask bleed at white content edges. In practice this
is subtle (the `binary_closing` step fills most holes), but it's a real issue for
designs that are predominantly white.

**Fix approaches** (none prototyped — all open design space):
- **Two-pass bounding region**: detect the overall patch outline first (the border
  rect is always dark, so it defines the bounds), then mask everything inside as
  content regardless of color. This is probably the cleanest approach.
- **Inject background color**: add a full-bleed rect with a known keying color
  (e.g., `#ff00ff`) as the LAST element in the SVG (behind everything). inkstitch
  will stitch it, making the background magenta instead of white. Then key on that
  exact color. Downside: wastes compute stitching a background you'll remove.
- **Local contrast**: instead of absolute RGB threshold, use local variance to
  distinguish "textured white thread" from "flat white background." Thread areas
  have stitch texture; background is flat.

**The magenta keying approach specifically**: we did NOT test whether inkstitch
renders elements behind the design area or clips to the content bounds. Worth
testing before committing to this approach.

### 4. vtracer SVG quirks
vtracer (v0.6.5) produces clean SVGs with these characteristics:
- **No `<g>` groups** — all paths are direct children of `<svg>`
- **No strokes** — every path uses `fill="..."` only
- **No opacity, visibility, or display attributes**
- **Every path has `transform="translate(x,y)"`** — this is the main quirk.
  vtracer offsets all paths with translate transforms instead of using absolute
  coordinates. inkstitch handles these transforms correctly.
- **No viewBox** — uses `width` and `height` in px only (e.g., `width="359" height="400"`)
- **No namespaces beyond default SVG**
- Fill colors are specified as `fill="#RRGGBB"` attributes, not in `style`

The `svg2patch.py` code handles all of these correctly. The fill:none / display:none
filter in the code won't trigger on vtracer output since vtracer doesn't produce
those patterns. The agent is correct that `opacity:0` and `visibility:hidden` aren't
filtered — but vtracer never produces them. If supporting arbitrary SVG inputs beyond
vtracer, those filters should be added.

### 5. postprocess.py is dead code
`attempts/inkstitched/postprocess.py` is an earlier, simpler post-processor
(fabric texture + thread grain + vignette). Nothing imports it. The active
post-processor is `normalmap/photorealistic.py` (764 lines, 13-stage pipeline).
There is only one copy of `photorealistic.py` — it lives at
`normalmap/photorealistic.py` and nowhere else. Safe to delete `postprocess.py`.

### 6. File structure migration
The implementing agent should **move files to the proposed structure** (not leave
them in attempts/inkstitched/). The imports will need updating:
- `pic2patch.py` imports `from svg2patch import add_inkstitch_params, render_inkstitch`
  — this works as-is if both files are in the same directory
- `pic2patch.py` and `svg2patch.py` both reference `normalmap/photorealistic.py` via
  `Path(__file__).parent / "normalmap" / "photorealistic.py"` — this path needs to
  change to match the new layout (e.g., `Path(__file__).parent / "photorealistic.py"`)

### 7. bin/generate wiring
The Ruby wrapper (`bin/generate` + `lib/patch_generator.rb`) is the old ImageMagick
pipeline. **Nothing external calls it today** — it's a local dev tool that Max runs
manually. No web service, no Slack bot, no CI. The calling convention is simple:
`bin/generate <input> <output>` or `bin/generate` (processes all images in
`test-inputs/` and writes to `tmp/`). No JSON output, no special exit codes.

Recommended approach: **rewrite bin/generate as a thin shell script** that calls
`python3 lib/pic2patch.py "$@"`. Keep `lib/patch_generator.rb` for reference but
don't try to maintain the Ruby path.

### 8. Border padding values
The 6% padding (`vb_w * 0.06`) and 80% corner radius (`pad * 0.8`) are
**"looked good" numbers** tuned visually against the NASA patch and raccoon test
images. There's no embroidery standard behind them. They produce a border that's
visible but not overwhelming, with corners that look like real patch rounding.
Feel free to tune if needed.

### 9. Other attempt directories
```
attempts/
├── embroidery-streamlines/  — Python streamline-tracing approach (has its own
│                              HANDOFF.md). Different technique: traces flow
│                              lines through the image. Interesting but slower
│                              and less realistic than inkstitch. Not referenced
│                              by anything in the current pipeline.
├── inkstitched/             — THE active pipeline (this is what we're shipping)
└── mrmo-stitch/             — Blender-based approach using MRMO-STITCH addon.
                               Just a .blend file and one test PNG. Dead end.
```
The `comfyui/` and `blender/` dirs inside `inkstitched/` are experimental
alternatives for the post-processing step. They produced good results but add
heavy dependencies. Keep for reference, don't delete, but don't wire into the
main pipeline.

## Reference outputs

The `batch/` directory contains test results. Best reference:
- `nasa_patch.svg` → hand-crafted NASA meatball, best quality baseline
- `03_hc_1_final.png` → pirate raccoon, full auto pipeline from PNG
- `06_hc_4_v2_final.png` → bowtie dino, clean cartoon style
