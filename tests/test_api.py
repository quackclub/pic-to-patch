"""
Integration tests for the pic-to-patch API.

These tests define the contract. When rewriting in Go/Ruby,
make all of these pass against the new server.

Requirements:
  - Server running at API_URL (default http://localhost:8000)
  - Redis running
  - Worker running

Run: pytest tests/test_api.py -v
"""

import os
import time
from pathlib import Path

import pytest
import requests

API_URL = os.environ.get("API_URL", "http://localhost:8000")
TEST_IMAGES = Path(__file__).parent.parent / "test-inputs"
POLL_INTERVAL = 2
POLL_TIMEOUT = 120


def api(path):
    return f"{API_URL}{path}"


@pytest.fixture
def sample_svg():
    svg = TEST_IMAGES / "nasa_logo.svg"
    assert svg.exists(), f"missing {svg}"
    return svg


@pytest.fixture
def sample_png():
    png = TEST_IMAGES / "nasa_logo.png"
    assert png.exists(), f"missing {png}"
    return png


def wait_for_job(job_id):
    deadline = time.time() + POLL_TIMEOUT
    while time.time() < deadline:
        r = requests.get(api(f"/jobs/{job_id}"))
        assert r.status_code == 200
        data = r.json()
        if data["status"] in ("complete", "failed"):
            return data
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"job {job_id} didn't complete in {POLL_TIMEOUT}s")


class TestHealth:
    def test_health_returns_ok(self):
        r = requests.get(api("/health"))
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


class TestPatchCreation:
    def test_submit_png_returns_job_id(self, sample_png):
        with open(sample_png, "rb") as f:
            r = requests.post(api("/patch"), files={"file": f})
        assert r.status_code == 200
        data = r.json()
        assert "job_id" in data
        assert len(data["job_id"]) > 0

    def test_full_pipeline_png(self, sample_png):
        with open(sample_png, "rb") as f:
            r = requests.post(api("/patch"), files={"file": f})
        job_id = r.json()["job_id"]

        result = wait_for_job(job_id)
        assert result["status"] == "complete", f"job failed: {result.get('error')}"
        assert "result_url" in result

    def test_result_is_valid_png(self, sample_png):
        with open(sample_png, "rb") as f:
            r = requests.post(api("/patch"), files={"file": f})
        job_id = r.json()["job_id"]
        wait_for_job(job_id)

        r = requests.get(api(f"/jobs/{job_id}/result"))
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"
        # PNG magic bytes
        assert r.content[:4] == b"\x89PNG"
        # sanity: output should be at least 10KB
        assert len(r.content) > 10_000

    def test_submit_svg(self, sample_svg):
        with open(sample_svg, "rb") as f:
            r = requests.post(api("/patch"), files={"file": ("input.svg", f, "image/svg+xml")})
        job_id = r.json()["job_id"]

        result = wait_for_job(job_id)
        assert result["status"] == "complete", f"job failed: {result.get('error')}"

    def test_custom_border_color(self, sample_png):
        with open(sample_png, "rb") as f:
            r = requests.post(
                api("/patch"),
                files={"file": f},
                data={"border_color": "#ff0000"},
            )
        job_id = r.json()["job_id"]
        result = wait_for_job(job_id)
        assert result["status"] == "complete"

    def test_no_postprocess(self, sample_png):
        with open(sample_png, "rb") as f:
            r = requests.post(
                api("/patch"),
                files={"file": f},
                data={"postprocess": "false"},
            )
        job_id = r.json()["job_id"]
        result = wait_for_job(job_id)
        assert result["status"] == "complete"


class TestJobStatus:
    def test_unknown_job_returns_404(self):
        r = requests.get(api("/jobs/nonexistent-id"))
        assert r.status_code == 404

    def test_missing_result_returns_404(self):
        r = requests.get(api("/jobs/nonexistent-id/result"))
        assert r.status_code == 404

    def test_processing_status_before_complete(self, sample_png):
        with open(sample_png, "rb") as f:
            r = requests.post(api("/patch"), files={"file": f})
        job_id = r.json()["job_id"]

        r = requests.get(api(f"/jobs/{job_id}"))
        assert r.status_code == 200
        assert r.json()["status"] in ("processing", "complete")
