"""
Core pipeline: image → embroidered patch PNG.

This is the throwaway python version. The interface is:
    convert(input_path, output_path, border_color="#0a0a14", color_precision=8)

That's it. Everything else is internal.
"""

import os
import subprocess
import tempfile
from pathlib import Path

from PIL import Image
import vtracer

from .svg2patch import add_inkstitch_params, render_inkstitch

MAX_IMAGE_DIM = int(os.environ.get("MAX_IMAGE_DIM", "500"))


def convert(input_path, output_path, border_color="#0a0a14", color_precision=8, postprocess=True):
    input_path = Path(input_path)
    output_path = Path(output_path)

    with tempfile.TemporaryDirectory(prefix="p2p_") as tmpdir:
        tmpdir = Path(tmpdir)

        resized = tmpdir / "resized.png"
        _resize(input_path, resized)

        vtracer_svg = tmpdir / "vectorized.svg"
        _vectorize(resized, vtracer_svg, color_precision)

        embroidery_svg = tmpdir / "embroidery.svg"
        add_inkstitch_params(vtracer_svg, embroidery_svg, border_color=border_color)

        stitch_png = tmpdir / "stitch.png"
        if not render_inkstitch(embroidery_svg, stitch_png):
            raise RuntimeError("inkstitch render failed")

        if postprocess:
            _postprocess(stitch_png, output_path)
        else:
            import shutil
            shutil.copy2(stitch_png, output_path)

    return output_path


def convert_svg(input_svg, output_path, border_color="#0a0a14", postprocess=True):
    input_svg = Path(input_svg)
    output_path = Path(output_path)

    with tempfile.TemporaryDirectory(prefix="p2p_") as tmpdir:
        tmpdir = Path(tmpdir)

        embroidery_svg = tmpdir / "embroidery.svg"
        add_inkstitch_params(input_svg, embroidery_svg, border_color=border_color)

        stitch_png = tmpdir / "stitch.png"
        if not render_inkstitch(embroidery_svg, stitch_png):
            raise RuntimeError("inkstitch render failed")

        if postprocess:
            _postprocess(stitch_png, output_path)
        else:
            import shutil
            shutil.copy2(stitch_png, output_path)

    return output_path


def _resize(input_path, output_path):
    img = Image.open(input_path)
    if img.mode == "P":
        img = img.convert("RGBA")
    w, h = img.size
    if max(w, h) > MAX_IMAGE_DIM:
        ratio = MAX_IMAGE_DIM / max(w, h)
        img = img.resize((int(w * ratio), int(h * ratio)), Image.Resampling.LANCZOS)
    img.save(str(output_path))


def _vectorize(input_image, output_svg, color_precision=8):
    vtracer.convert_image_to_svg_py(
        image_path=str(input_image),
        out_path=str(output_svg),
        colormode="color",
        hierarchical="stacked",
        filter_speckle=4,
        color_precision=color_precision,
        corner_threshold=60,
        length_threshold=4,
        splice_threshold=45,
    )
    if not output_svg.exists():
        raise RuntimeError("vtracer produced no output")


def _postprocess(stitch_png, output_path):
    script = Path(__file__).parent / "photorealistic.py"
    if not script.exists():
        import shutil
        shutil.copy2(stitch_png, output_path)
        return

    import sys
    result = subprocess.run(
        [sys.executable, str(script), str(stitch_png), str(output_path)],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0 or not output_path.exists():
        import shutil
        shutil.copy2(stitch_png, output_path)
