"""Hugging Face Space: a read-only demo of the rtdp read API. SYNTHETIC DATA ONLY.

At startup the Space ingests two seeded synthetic batches into a throwaway ``file://`` warehouse
(the same ``run_ingest`` path as ``rtdp ingest --source synthetic``; the file:// backend is a
dev/CI convenience only, not the target architecture), builds the real FastAPI app with
``rtdp.api.app.create_app``, and talks to it through ``fastapi.testclient.TestClient``: the same
routes, in-process, no network. Every button below calls one read-only GET endpoint and shows the
endpoint it called next to the JSON it got back. The agent is not exposed (no model, no key), and
there is no write path in this UI.

Run locally:  python deploy/hf-space/app.py   (needs gradio; see README.md in this folder)
"""

from __future__ import annotations

import gc
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any

try:  # ZeroGPU Spaces refuse to start without a @spaces.GPU function; elsewhere `spaces` is absent
    import spaces

    _gpu = spaces.GPU
except ImportError:

    def _gpu(*args: Any, **kwargs: Any):
        if len(args) == 1 and callable(args[0]) and not kwargs:
            return args[0]
        return lambda fn: fn


@_gpu(duration=1)
def _startup_check_stub() -> None:
    """Never called. It exists only so a ZeroGPU Space passes its startup check; nothing here
    uses a GPU."""


SEEDS = (42, 43)
ROWS_PER_BATCH = 50
LATEST = "latest"


class Backend:
    """The throwaway warehouse plus the in-process read API, with a lock around every call."""

    def __init__(self, workdir: Path | None = None) -> None:
        from fastapi.testclient import TestClient

        from rtdp.api.app import create_app
        from rtdp.config import Settings
        from rtdp.ingest import run_ingest
        from rtdp.sources.synthetic import SyntheticSource

        self.workdir = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="rtdp-space-"))
        settings = Settings(
            _env_file=None,
            storage_backend="file",
            local_warehouse_dir=self.workdir / "warehouse",
            catalog_db_path=self.workdir / "warehouse" / "catalog.db",
        )
        self.ingest_summary: list[dict] = []
        for seed in SEEDS:
            result = run_ingest(settings, SyntheticSource(n_rows=ROWS_PER_BATCH, seed=seed))
            self.ingest_summary.append(
                {
                    "seed": seed,
                    "rows_in_batch": result.dq.n_rows,
                    "rows_written": result.rows_written,
                    "dq_failure_cases": result.dq.n_failures,
                    "dq_warning_cases": result.dq.n_warnings,
                }
            )
        self._lock = threading.Lock()
        self._client = TestClient(create_app(settings))
        self._client.__enter__()  # runs the app lifespan (builds the catalog once)

    def call(self, path: str, **params: Any) -> dict:
        """One read-only GET through the real routes. Returns the endpoint called and the reply."""
        clean = {k: v for k, v in params.items() if v is not None}
        with self._lock:
            resp = self._client.get(path, params=clean)
        url = resp.request.url
        endpoint = f"GET {url.path}" + (f"?{url.query.decode()}" if url.query else "")
        try:
            body = resp.json()
        except ValueError:
            body = {"raw": resp.text}
        return {"endpoint": endpoint, "status": resp.status_code, "response": body}

    def snapshot_choices(self) -> list[str]:
        snapshots = self.call("/snapshots")["response"]
        ids = [str(s["snapshot_id"]) for s in snapshots] if isinstance(snapshots, list) else []
        return [LATEST, *ids]

    def dq_summary(self) -> dict:
        return {
            "endpoint": "computed at ingest (not an API endpoint)",
            "status": None,
            "response": {
                "note": "Stage 1 data-quality gate results for the two startup batches",
                "batches": self.ingest_summary,
            },
        }

    def close(self) -> None:
        try:
            self._client.__exit__(None, None, None)
        finally:
            gc.collect()  # release SQLite's handle first; Windows cannot delete an open file
            shutil.rmtree(self.workdir, ignore_errors=True)


def _as_snapshot_id(choice: str | None) -> int | None:
    return None if not choice or choice == LATEST else int(choice)


def build_ui(backend: Backend):
    """The Gradio Blocks UI (gradio is imported here so the backend works without it)."""
    import gradio as gr

    def show(result: dict) -> tuple[str, dict]:
        status = f" -> HTTP {result['status']}" if result["status"] is not None else ""
        return f"{result['endpoint']}{status}", result["response"]

    def health() -> tuple[str, dict]:
        return show(backend.call("/health"))

    def livez() -> tuple[str, dict]:
        return show(backend.call("/livez"))

    def snapshots() -> tuple[str, dict]:
        return show(backend.call("/snapshots"))

    def counts(interval: str, by_country: bool, snapshot: str) -> tuple[str, dict]:
        return show(
            backend.call(
                "/stats/flights-per-interval",
                interval=interval,
                group_by="origin_country" if by_country else None,
                as_of_snapshot_id=_as_snapshot_id(snapshot),
            )
        )

    def flights(limit: int, snapshot: str) -> tuple[str, dict]:
        return show(
            backend.call("/flights", limit=int(limit), as_of_snapshot_id=_as_snapshot_id(snapshot))
        )

    def dq() -> tuple[str, dict]:
        return show(backend.dq_summary())

    with gr.Blocks(title="rtdp read API demo (synthetic data)") as demo:
        gr.Markdown(
            "# rtdp read API demo\n"
            "**Synthetic data only.** Two seeded synthetic batches were ingested into an "
            "Apache Iceberg table when this Space started. Each button calls one read-only "
            "endpoint of the real FastAPI app, in-process. The agent is not exposed here, and "
            "nothing on this page writes data. The warehouse lives on a local `file://` path, "
            "a dev/CI convenience only, not the target architecture."
        )
        called = gr.Textbox(label="Endpoint called", interactive=False)
        reply = gr.JSON(label="Response")
        outputs = [called, reply]
        snapshot = gr.Dropdown(
            choices=backend.snapshot_choices(), value=LATEST, label="Read snapshot (time travel)"
        )
        with gr.Row():
            gr.Button("/health").click(health, None, outputs, api_name="health")
            gr.Button("/livez").click(livez, None, outputs, api_name="livez")
            gr.Button("/snapshots").click(snapshots, None, outputs, api_name="snapshots")
            gr.Button("Data-quality summary (at ingest)").click(dq, None, outputs, api_name="dq")
        with gr.Row():
            interval = gr.Radio(["day", "hour"], value="day", label="Interval")
            by_country = gr.Checkbox(value=False, label="Group by origin country")
            gr.Button("/stats/flights-per-interval").click(
                counts, [interval, by_country, snapshot], outputs, api_name="counts"
            )
        with gr.Row():
            limit = gr.Slider(1, 20, value=5, step=1, label="Rows")
            gr.Button("/flights").click(flights, [limit, snapshot], outputs, api_name="flights")
    return demo


if __name__ == "__main__":
    build_ui(Backend()).launch()
