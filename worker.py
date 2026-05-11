"""
pic-to-patch worker. Picks jobs from Redis queue and runs the pipeline.

Run with: rq worker patches --url redis://localhost:6379
"""

from pathlib import Path
from pipeline.convert import convert, convert_svg


def run_pipeline(input_path, output_path, border_color="#0a0a14",
                 color_precision=8, postprocess=True, is_svg=False, job_id=None):
    error_file = Path(output_path).parent / "error.txt"
    try:
        if is_svg:
            convert_svg(input_path, output_path,
                        border_color=border_color, postprocess=postprocess)
        else:
            convert(input_path, output_path,
                    border_color=border_color, color_precision=color_precision,
                    postprocess=postprocess)
    except Exception as e:
        error_file.write_text(str(e))
        raise
