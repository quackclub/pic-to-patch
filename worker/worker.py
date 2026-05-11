import os
import json
import tempfile
from pathlib import Path

import redis

from pipeline.convert import convert, convert_svg

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
RESULT_TTL = int(os.environ.get("RESULT_TTL", "3600"))
QUEUE_NAME = "patches"


def main():
    conn = redis.from_url(REDIS_URL)
    conn.ping()
    print(f"Worker ready, listening on queue '{QUEUE_NAME}'")

    while True:
        _, payload_bytes = conn.brpop(QUEUE_NAME)
        job = json.loads(payload_bytes)
        job_id = job["job_id"]
        print(f"Processing {job_id}")

        try:
            run_pipeline(
                conn, job_id,
                border_color=job.get("border_color", "#0a0a14"),
                color_precision=job.get("color_precision", 8),
                postprocess=job.get("postprocess", True),
            )
            print(f"  done")
        except Exception as e:
            print(f"  failed: {e}")


def run_pipeline(conn, job_id, border_color="#0a0a14",
                 color_precision=8, postprocess=True):
    try:
        input_bytes = conn.get(f"job:{job_id}:input")
        ext = (conn.get(f"job:{job_id}:ext") or b"png").decode()
        if not input_bytes:
            raise RuntimeError("input not found in redis")

        is_svg = ext.lower() == "svg"

        with tempfile.TemporaryDirectory(prefix="p2p_") as tmpdir:
            tmpdir = Path(tmpdir)
            input_path = tmpdir / f"input.{ext}"
            input_path.write_bytes(input_bytes)
            output_path = tmpdir / "patch.png"

            if is_svg:
                convert_svg(str(input_path), str(output_path),
                            border_color=border_color, postprocess=postprocess)
            else:
                convert(str(input_path), str(output_path),
                        border_color=border_color,
                        color_precision=color_precision,
                        postprocess=postprocess)

            result_bytes = output_path.read_bytes()

        conn.setex(f"job:{job_id}:result", RESULT_TTL, result_bytes)
        conn.setex(f"job:{job_id}:status", RESULT_TTL, "complete")
        conn.delete(f"job:{job_id}:input")

    except Exception as e:
        conn.setex(f"job:{job_id}:status", RESULT_TTL, "failed")
        conn.setex(f"job:{job_id}:error", RESULT_TTL, str(e))
        raise


if __name__ == "__main__":
    main()
