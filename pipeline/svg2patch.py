#!/usr/bin/env python3
"""
svg2patch: Add inkstitch embroidery parameters to any SVG and render as a patch.

Takes a clean SVG (from vtracer, manual design, etc.) and:
  1. Adds inkstitch namespace + fill parameters to all paths
  2. Sets document dimensions to mm
  3. Renders with inkstitch for realistic stitch simulation
  4. Optionally runs photorealistic post-processing
"""

import sys
import os
import subprocess
import argparse
from pathlib import Path
from lxml import etree

INKSTITCH_NS = "http://inkstitch.org/namespace"
SVG_NS = "http://www.w3.org/2000/svg"
INKSCAPE_NS = "http://www.inkscape.org/namespaces/inkscape"
SODIPODI_NS = "http://sodipodi.sourceforge.net/DTD/sodipodi-0.0.dtd"

INKSTITCH_BIN = os.environ.get(
    "INKSTITCH_BIN",
    os.path.expanduser(
        "~/Library/Application Support/org.inkscape.Inkscape"
        "/config/inkscape/extensions/inkstitch.app/Contents/MacOS/inkstitch"
    ),
)

PATCH_WIDTH_MM = 80.0


def add_inkstitch_params(svg_path, output_svg_path, border_color=None):
    """Add inkstitch embroidery parameters to all paths in an SVG."""
    etree.register_namespace("inkstitch", INKSTITCH_NS)
    etree.register_namespace("inkscape", INKSCAPE_NS)
    etree.register_namespace("sodipodi", SODIPODI_NS)

    tree = etree.parse(str(svg_path))
    root = tree.getroot()

    nsmap = dict(root.nsmap)
    nsmap["inkstitch"] = INKSTITCH_NS
    nsmap["inkscape"] = INKSCAPE_NS
    nsmap["sodipodi"] = SODIPODI_NS
    new_root = etree.Element(root.tag, nsmap=nsmap)
    new_root.attrib.update(root.attrib)
    new_root.text = root.text
    new_root.tail = root.tail
    for child in root:
        new_root.append(child)
    root = new_root

    # get original dimensions from viewBox or width/height
    viewbox = root.get("viewBox")
    if viewbox:
        parts = viewbox.split()
        vb_w = float(parts[2]) - float(parts[0])
        vb_h = float(parts[3]) - float(parts[1])
    else:
        vb_w = float(root.get("width", "100").replace("px", "").replace("mm", ""))
        vb_h = float(root.get("height", "100").replace("px", "").replace("mm", ""))

    # set dimensions to mm
    scale = PATCH_WIDTH_MM / vb_w
    width_mm = PATCH_WIDTH_MM
    height_mm = vb_h * scale
    root.set("width", f"{width_mm}mm")
    root.set("height", f"{height_mm}mm")

    # add namedview
    existing_nv = root.find("{%s}namedview" % SODIPODI_NS)
    if existing_nv is None:
        nv = etree.SubElement(root, "{%s}namedview" % SODIPODI_NS)
        nv.set("{%s}document-units" % INKSCAPE_NS, "mm")

    # add version metadata
    existing_meta = root.find("{%s}metadata" % SVG_NS)
    if existing_meta is None:
        existing_meta = root.find("metadata")
    if existing_meta is None:
        existing_meta = etree.SubElement(root, "metadata")
    version_el = existing_meta.find("{%s}inkstitch_svg_version" % INKSTITCH_NS)
    if version_el is None:
        version_el = etree.SubElement(existing_meta, "{%s}inkstitch_svg_version" % INKSTITCH_NS)
    version_el.text = "3"

    # find all paths/shapes and add inkstitch params
    all_elements = root.iter()
    shape_tags = {
        "{%s}path" % SVG_NS, "{%s}circle" % SVG_NS, "{%s}ellipse" % SVG_NS,
        "{%s}rect" % SVG_NS, "{%s}polygon" % SVG_NS,
        "path", "circle", "ellipse", "rect", "polygon",
    }

    element_count = 0
    for el in all_elements:
        tag = el.tag
        if tag not in shape_tags:
            continue

        # get fill color from style or fill attribute
        style = el.get("style", "")
        fill = el.get("fill", "")

        if "fill:none" in style or fill == "none":
            continue
        if "display:none" in style:
            continue

        # convert fill attribute to style if needed
        if fill and "fill:" not in style:
            if style:
                el.set("style", f"fill:{fill};stroke:none;{style}")
            else:
                el.set("style", f"fill:{fill};stroke:none")
            if el.get("fill"):
                del el.attrib["fill"]
        elif not fill and "fill:" not in style:
            continue

        # ensure stroke:none is in style
        current_style = el.get("style", "")
        if "stroke:" not in current_style:
            el.set("style", current_style.rstrip(";") + ";stroke:none")

        # add inkstitch fill parameters
        angle = (30 + element_count * 23) % 180
        el.set("{%s}fill_method" % INKSTITCH_NS, "auto_fill")
        el.set("{%s}fill_underlay" % INKSTITCH_NS, "true")
        el.set("{%s}fill_underlay_angle" % INKSTITCH_NS, str((angle + 90) % 360))
        el.set("{%s}angle" % INKSTITCH_NS, str(angle))
        el.set("{%s}row_spacing_mm" % INKSTITCH_NS, "0.25")
        el.set("{%s}max_stitch_length_mm" % INKSTITCH_NS, "3.0")
        el.set("{%s}staggers" % INKSTITCH_NS, "4")

        element_count += 1

    # add border if requested
    if border_color:
        if viewbox:
            parts = [float(p) for p in viewbox.split()]
            origin_x, origin_y = parts[0], parts[1]
        else:
            origin_x, origin_y = 0.0, 0.0

        pad = vb_w * 0.06
        border = etree.Element("rect")
        border.set("x", str(origin_x - pad))
        border.set("y", str(origin_y - pad))
        border.set("width", str(vb_w + pad * 2))
        border.set("height", str(vb_h + pad * 2))
        border.set("rx", str(pad * 0.8))
        border.set("ry", str(pad * 0.8))
        border.set("style", f"fill:{border_color};stroke:none")
        border.set("{%s}fill_method" % INKSTITCH_NS, "auto_fill")
        border.set("{%s}fill_underlay" % INKSTITCH_NS, "true")
        border.set("{%s}angle" % INKSTITCH_NS, "90")
        border.set("{%s}row_spacing_mm" % INKSTITCH_NS, "0.2")
        border.set("{%s}max_stitch_length_mm" % INKSTITCH_NS, "2.5")
        border.set("{%s}staggers" % INKSTITCH_NS, "4")
        root.insert(0, border)

        root.set("viewBox", f"{origin_x-pad} {origin_y-pad} {vb_w+pad*2} {vb_h+pad*2}")
        new_scale = PATCH_WIDTH_MM / (vb_w + pad * 2)
        root.set("width", f"{(vb_w + pad*2) * new_scale}mm")
        root.set("height", f"{(vb_h + pad*2) * new_scale}mm")
        element_count += 1

    etree.ElementTree(root).write(str(output_svg_path), xml_declaration=True, encoding="utf-8", pretty_print=True)
    print(f"  {element_count} elements parameterized")
    return output_svg_path


def render_inkstitch(svg_path, output_png):
    """Render with inkstitch realistic PNG."""
    with open(output_png, "wb") as f:
        result = subprocess.run(
            [INKSTITCH_BIN, "--extension=png_realistic", str(svg_path)],
            stdout=f, stderr=subprocess.PIPE, timeout=300,
        )
    if result.returncode != 0:
        print(f"  inkstitch error: {result.stderr.decode()[:300]}", file=sys.stderr)
        if output_png.exists():
            output_png.unlink()
        return False
    return output_png.exists() and output_png.stat().st_size > 0


def main():
    parser = argparse.ArgumentParser(description="Add inkstitch params to SVG and render as patch")
    parser.add_argument("input", help="Input SVG path")
    parser.add_argument("output", nargs="?", help="Output PNG path")
    parser.add_argument("-b", "--border-color", default="#0a0a14", help="Border color (default: #0a0a14)")
    parser.add_argument("--no-border", action="store_true", help="Skip border")
    parser.add_argument("--no-postprocess", action="store_true", help="Skip photorealistic post-processing")
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output) if args.output else input_path.with_name(input_path.stem + "_patch.png")

    border = None if args.no_border else args.border_color

    print(f"Adding inkstitch params to {input_path}...")
    embroidery_svg = output_path.with_suffix(".svg")
    add_inkstitch_params(input_path, embroidery_svg, border_color=border)

    print(f"Rendering with inkstitch...")
    if render_inkstitch(embroidery_svg, output_path):
        print(f"  Stitch render: {output_path} ({output_path.stat().st_size // 1024}KB)")
    else:
        print("  inkstitch render failed")
        return

    if not args.no_postprocess:
        postprocess_script = Path(__file__).parent / "photorealistic.py"
        if postprocess_script.exists():
            final_path = output_path.with_name(output_path.stem.replace("_patch", "") + "_final.png")
            print(f"Post-processing...")
            subprocess.run(
                [sys.executable, str(postprocess_script), str(output_path), str(final_path)],
                timeout=120,
            )
            if final_path.exists():
                print(f"  Final: {final_path}")
        else:
            print("  (photorealistic.py not found, skipping post-processing)")

    print("Done!")


if __name__ == "__main__":
    main()
