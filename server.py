"""
pic-to-patch API server.

POST /patch          — upload image, returns {job_id}
GET  /jobs/{job_id}  — returns {status, result_url?}
GET  /jobs/{job_id}/result — returns the PNG directly

Jobs are stored in Redis. Results are stored on disk (mount a PVC in k8s).
"""

import os
import uuid
import shutil
import tempfile
from pathlib import Path

from fastapi import FastAPI, UploadFile, HTTPException
from fastapi.responses import FileResponse
import redis
import rq

RESULTS_DIR = Path(os.environ.get("RESULTS_DIR", "/tmp/p2p_results"))
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")

app = FastAPI(title="pic-to-patch", version="0.1.0")
redis_conn = redis.from_url(REDIS_URL)
queue = rq.Queue("patches", connection=redis_conn)


@app.post("/patch")
async def create_patch(
    file: UploadFile,
    border_color: str = "#0a0a14",
    color_precision: int = 8,
    postprocess: bool = True,
):
    job_id = str(uuid.uuid4())
    job_dir = RESULTS_DIR / job_id
    job_dir.mkdir()

    ext = Path(file.filename or "input.png").suffix or ".png"
    input_path = job_dir / f"input{ext}"
    with open(input_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    output_path = job_dir / "patch.png"

    is_svg = ext.lower() == ".svg"

    queue.enqueue(
        "worker.run_pipeline",
        job_id=job_id,
        input_path=str(input_path),
        output_path=str(output_path),
        border_color=border_color,
        color_precision=color_precision,
        postprocess=postprocess,
        is_svg=is_svg,
        job_timeout=300,
        result_ttl=3600,
        meta={"job_id": job_id},
    )

    return {"job_id": job_id}


@app.get("/jobs/{job_id}")
async def get_job(job_id: str):
    job_dir = RESULTS_DIR / job_id
    if not job_dir.exists():
        raise HTTPException(404, "job not found")

    output = job_dir / "patch.png"
    error_file = job_dir / "error.txt"

    if error_file.exists():
        return {"status": "failed", "error": error_file.read_text()}
    if output.exists() and output.stat().st_size > 0:
        return {"status": "complete", "result_url": f"/jobs/{job_id}/result"}

    return {"status": "processing"}


@app.get("/jobs/{job_id}/result")
async def get_result(job_id: str):
    output = RESULTS_DIR / job_id / "patch.png"
    if not output.exists():
        raise HTTPException(404, "result not ready")
    return FileResponse(output, media_type="image/png", filename=f"{job_id}.png")


@app.get("/health")
async def health():
    try:
        redis_conn.ping()
    except Exception:
        raise HTTPException(503, "redis unavailable")
    return {"status": "ok"}
