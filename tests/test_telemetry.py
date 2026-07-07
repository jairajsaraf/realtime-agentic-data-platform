"""Telemetry boundary: no-op + dependency-free by default; graceful when the extra is absent.

These tests run on the default install (no ``[otel]`` extra), exactly like CI. They assert the
disabled path imports no opentelemetry package and that an enabled-but-missing-extra
configuration degrades to no-op instead of breaking the app.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import sys

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rtdp import telemetry
from rtdp.agent.tools import ApiToolExecutor, static_registry
from rtdp.api import create_app
from rtdp.catalog import build_catalog
from rtdp.config import LogFormat
from rtdp.ingest import run_ingest
from rtdp.sources.synthetic import SyntheticSource

# The OTel-enabled branches below only execute when the optional `[otel]` extra is installed.
# Gate them so the default key-free suite (`pytest -m "not localstack"`) stays green by skipping
# them, while the CI `[otel]` job (`uv run --extra otel -- pytest tests/test_telemetry.py`) runs
# them. None of these tests emit spans, so nothing is ever exported over the network.
_OTEL_INSTALLED = importlib.util.find_spec("opentelemetry") is not None
otel_required = pytest.mark.skipif(
    not _OTEL_INSTALLED, reason="requires the optional [otel] extra"
)


def test_otel_and_log_settings_default_off(file_settings):
    assert file_settings.otel_enabled is False
    assert file_settings.otel_service_name == "rtdp"
    assert file_settings.otel_exporter_otlp_endpoint is None
    assert file_settings.log_format is LogFormat.TEXT
    assert file_settings.log_level == "INFO"


def test_init_telemetry_noop_and_dependency_free_when_disabled(file_settings):
    had_otel = "opentelemetry" in sys.modules
    assert telemetry.init_telemetry(file_settings) is False
    # The disabled path must not import any opentelemetry package.
    if not had_otel:
        assert "opentelemetry" not in sys.modules


def test_instrument_fastapi_noop_when_disabled(file_settings):
    app = FastAPI()
    assert telemetry.instrument_fastapi(app, file_settings) is False


def test_init_telemetry_degrades_when_enabled_but_extra_missing(file_settings, monkeypatch):
    # Force the optional extra to look absent even if it happens to be installed locally.
    monkeypatch.setattr(telemetry, "_tracing_ready", False)
    monkeypatch.setitem(sys.modules, "opentelemetry", None)
    enabled = file_settings.model_copy(update={"otel_enabled": True})
    assert telemetry.init_telemetry(enabled) is False  # graceful no-op, no exception


def test_create_app_boots_when_otel_enabled_but_extra_missing(file_settings, monkeypatch):
    monkeypatch.setattr(telemetry, "_tracing_ready", False)
    monkeypatch.setitem(sys.modules, "opentelemetry", None)
    enabled = file_settings.model_copy(update={"otel_enabled": True})
    app = create_app(enabled)  # must not raise
    with TestClient(app) as client:
        assert client.get("/health").status_code in (200, 503)


def test_create_app_works_with_telemetry_disabled(file_settings):
    app = create_app(file_settings)
    with TestClient(app) as client:
        assert client.get("/health").status_code in (200, 503)


def test_json_formatter_emits_valid_json():
    record = logging.LogRecord("rtdp.test", logging.INFO, __file__, 10, "hi %s", ("there",), None)
    parsed = json.loads(telemetry._JsonFormatter().format(record))
    assert parsed["msg"] == "hi there"
    assert parsed["level"] == "INFO"
    assert parsed["logger"] == "rtdp.test"


def test_configure_logging_is_idempotent(file_settings):
    logger = telemetry.configure_logging(file_settings)
    first = sum(getattr(h, telemetry._OUR_HANDLER_FLAG, False) for h in logger.handlers)
    telemetry.configure_logging(file_settings)
    second = sum(getattr(h, telemetry._OUR_HANDLER_FLAG, False) for h in logger.handlers)
    assert first == 1
    assert second == 1  # no duplicate handler accumulation


def test_configure_logging_json_format_uses_json_formatter(file_settings):
    settings = file_settings.model_copy(update={"log_format": LogFormat.JSON})
    logger = telemetry.configure_logging(settings)
    ours = [h for h in logger.handlers if getattr(h, telemetry._OUR_HANDLER_FLAG, False)]
    assert any(isinstance(h.formatter, telemetry._JsonFormatter) for h in ours)


def test_json_formatter_includes_exception_when_present():
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord(
            "rtdp.test", logging.ERROR, __file__, 10, "failed", (), sys.exc_info()
        )
    parsed = json.loads(telemetry._JsonFormatter().format(record))
    assert "exc" in parsed
    assert "ValueError" in parsed["exc"]


# --- custom span() helper: no-op / dependency-free path (default install, no extra) ---


def _ok_flights_response(request):
    return httpx.Response(200, json={"snapshot_id": 42, "count": 1, "items": [{"icao24": "abc"}]})


def _mock_executor(handler):
    return ApiToolExecutor(
        "http://test", httpx.Client(transport=httpx.MockTransport(handler)), static_registry()
    )


def test_span_noop_and_dependency_free_when_disabled(monkeypatch):
    monkeypatch.setattr(telemetry, "_tracing_ready", False)
    had_otel = "opentelemetry" in sys.modules
    with telemetry.span("rtdp.test", rows_in=1) as s:
        s.set_attribute("rows_written", 1)
        s.set_attribute("http.status_code", None)  # None is safely skipped, no error
    # The disabled path must not import any opentelemetry package.
    if not had_otel:
        assert "opentelemetry" not in sys.modules


def test_run_ingest_unchanged_with_span_disabled(file_settings, monkeypatch):
    monkeypatch.setattr(telemetry, "_tracing_ready", False)
    res = run_ingest(file_settings, SyntheticSource(n_rows=20, seed=1, inject_warnings=False))
    assert res.rows_in == 20
    assert res.rows_written == 20
    assert res.snapshot_count == 1
    assert res.snapshot_id is not None
    assert res.dq.ok is True
    table = build_catalog(file_settings).load_table(file_settings.table_identifier)
    assert table.scan().to_arrow().num_rows == 20


def test_execute_unchanged_with_span_disabled(monkeypatch):
    monkeypatch.setattr(telemetry, "_tracing_ready", False)
    result = _mock_executor(_ok_flights_response).execute("flights", {"icao24": "abc"})
    assert result.ok is True
    assert result.status_code == 200
    assert result.snapshot_id == 42
    assert result.data["items"][0]["icao24"] == "abc"
    assert result.error is None


def test_ingest_lag_seconds_omitted_on_missing_or_invalid_contact():
    """The lag is computed only when EVERY row has a valid numeric last_contact; a single
    missing/invalid value yields None so the span omits ``ingest.lag_seconds`` entirely."""
    from datetime import UTC, datetime

    from rtdp.ingest import _ingest_lag_seconds

    t = datetime(2026, 1, 1, tzinfo=UTC)
    base = t.timestamp()
    # All rows valid -> lag against the newest (max) last_contact.
    assert _ingest_lag_seconds(
        t, [{"last_contact": base - 9}, {"last_contact": base - 5}]
    ) == pytest.approx(5.0)
    # Empty batch -> None.
    assert _ingest_lag_seconds(t, []) is None
    # One row missing last_contact -> None (no partial-batch calculation).
    assert _ingest_lag_seconds(t, [{"last_contact": base - 5}, {}]) is None
    # One non-numeric value -> None.
    assert _ingest_lag_seconds(t, [{"last_contact": base - 5}, {"last_contact": "nope"}]) is None
    # A bool is not numeric here -> None.
    assert _ingest_lag_seconds(t, [{"last_contact": True}]) is None


# --- OTel-enabled branches (run only under the optional [otel] extra; never export spans) ---


@otel_required
def test_setup_tracing_enabled_initializes(file_settings, monkeypatch):
    monkeypatch.setattr(telemetry, "_tracing_ready", False)
    enabled = file_settings.model_copy(update={"otel_enabled": True})
    assert telemetry._setup_tracing(enabled) is True
    assert telemetry._tracing_ready is True


@otel_required
def test_setup_tracing_honors_custom_endpoint(file_settings, monkeypatch):
    monkeypatch.setattr(telemetry, "_tracing_ready", False)
    enabled = file_settings.model_copy(
        update={"otel_enabled": True, "otel_exporter_otlp_endpoint": "http://localhost:4317"}
    )
    assert telemetry._setup_tracing(enabled) is True  # exporter built with the explicit endpoint


@otel_required
def test_setup_tracing_idempotent_when_ready(file_settings, monkeypatch):
    monkeypatch.setattr(telemetry, "_tracing_ready", True)
    enabled = file_settings.model_copy(update={"otel_enabled": True})
    assert telemetry._setup_tracing(enabled) is True  # early return; no re-initialization


@otel_required
def test_init_telemetry_enabled_initializes_tracing(file_settings, monkeypatch):
    monkeypatch.setattr(telemetry, "_tracing_ready", False)
    enabled = file_settings.model_copy(update={"otel_enabled": True})
    assert telemetry.init_telemetry(enabled) is True


@otel_required
def test_instrument_fastapi_enabled_instruments_app(file_settings, monkeypatch):
    monkeypatch.setattr(telemetry, "_tracing_ready", False)
    enabled = file_settings.model_copy(update={"otel_enabled": True})
    app = FastAPI()
    assert telemetry.instrument_fastapi(app, enabled) is True


@otel_required
def test_instrument_fastapi_skips_when_instrumentation_missing(file_settings, monkeypatch):
    # Tracing sets up fine, but force the FastAPI instrumentation import to fail.
    monkeypatch.setattr(telemetry, "_tracing_ready", False)
    monkeypatch.setitem(sys.modules, "opentelemetry.instrumentation.fastapi", None)
    enabled = file_settings.model_copy(update={"otel_enabled": True})
    app = FastAPI()
    assert telemetry.instrument_fastapi(app, enabled) is False


# --- custom span() helper: enabled path (spans asserted via InMemorySpanExporter, no network) ---


def _provider_and_exporter():
    """Fresh in-memory TracerProvider + exporter (asserts spans, no network export)."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def _install_test_tracer(monkeypatch, provider):
    """Point telemetry.span() at `provider`'s tracer (bypasses the set-once global provider)."""
    monkeypatch.setattr(telemetry, "_tracing_ready", True)
    monkeypatch.setattr(
        "opentelemetry.trace.get_tracer", lambda *a, **k: provider.get_tracer("rtdp")
    )


@otel_required
def test_span_emits_named_span_when_enabled(monkeypatch):
    provider, exporter = _provider_and_exporter()
    _install_test_tracer(monkeypatch, provider)
    with telemetry.span("rtdp.unit.test", rows_in=3) as s:
        s.set_attribute("rows_written", 3)
        s.set_attribute("dropped", None)  # None is skipped, not exported
    spans = exporter.get_finished_spans()
    assert [sp.name for sp in spans] == ["rtdp.unit.test"]
    attrs = dict(spans[0].attributes)
    assert attrs["rows_in"] == 3
    assert attrs["rows_written"] == 3
    assert "dropped" not in attrs


@otel_required
def test_span_records_and_reraises_body_exception(monkeypatch):
    provider, exporter = _provider_and_exporter()
    _install_test_tracer(monkeypatch, provider)
    with pytest.raises(ValueError):
        with telemetry.span("rtdp.boom"):
            raise ValueError("boom")
    # The wrapped-body exception propagated unchanged, and the span still finished/exported.
    assert [sp.name for sp in exporter.get_finished_spans()] == ["rtdp.boom"]


@otel_required
def test_span_falls_back_to_noop_when_span_start_raises(monkeypatch):
    # A broken OTel setup where STARTING the span raises (e.g. a processor whose on_start errors):
    # span() must swallow it, fall back to no-op, and still run the wrapped body unchanged.
    monkeypatch.setattr(telemetry, "_tracing_ready", True)

    class _BoomCM:
        def __enter__(self):
            raise RuntimeError("span start boom")

        def __exit__(self, *exc):
            return False

    class _BoomTracer:
        def start_as_current_span(self, name):
            return _BoomCM()

    monkeypatch.setattr("opentelemetry.trace.get_tracer", lambda *a, **k: _BoomTracer())
    ran = []
    with telemetry.span("rtdp.guard", rows_in=1) as s:
        s.set_attribute("rows_written", 1)  # no-op span; must not raise
        ran.append(True)
    assert ran == [True]  # wrapped body executed despite the span-start failure


@otel_required
def test_ingest_batch_span_emitted_when_enabled(file_settings, monkeypatch):
    from datetime import UTC, datetime

    provider, exporter = _provider_and_exporter()
    _install_test_tracer(monkeypatch, provider)
    ingest_time = datetime(2026, 1, 1, tzinfo=UTC)
    res = run_ingest(
        file_settings,
        SyntheticSource(n_rows=10, seed=1, inject_warnings=False),
        ingest_time=ingest_time,
    )
    assert res.rows_written == 10
    spans = [sp for sp in exporter.get_finished_spans() if sp.name == "rtdp.ingest.batch"]
    assert len(spans) == 1
    attrs = dict(spans[0].attributes)
    assert attrs["rows_in"] == 10
    assert attrs["rows_written"] == 10
    # lag uses the INJECTED ingest_time, not wall-clock now: verify against the appended rows.
    table = build_catalog(file_settings).load_table(file_settings.table_identifier)
    rows = table.scan().to_arrow().to_pylist()
    expected = ingest_time.timestamp() - max(r["last_contact"] for r in rows)
    assert attrs["ingest.lag_seconds"] == pytest.approx(expected)


@otel_required
def test_ingest_batch_span_omitted_on_dq_failure(file_settings, monkeypatch):
    provider, exporter = _provider_and_exporter()
    _install_test_tracer(monkeypatch, provider)
    res = run_ingest(
        file_settings,
        SyntheticSource(n_rows=5, seed=1, inject_warnings=False, inject_failures=True),
    )
    assert res.dq.ok is False
    assert res.rows_written == 0
    assert [sp for sp in exporter.get_finished_spans() if sp.name == "rtdp.ingest.batch"] == []


@otel_required
def test_tool_call_span_success(monkeypatch):
    provider, exporter = _provider_and_exporter()
    _install_test_tracer(monkeypatch, provider)
    result = _mock_executor(_ok_flights_response).execute("flights", {"icao24": "abc"})
    assert result.ok is True
    spans = [sp for sp in exporter.get_finished_spans() if sp.name == "rtdp.agent.tool_call"]
    assert len(spans) == 1
    attrs = dict(spans[0].attributes)
    assert attrs["name"] == "flights"
    assert attrs["ok"] is True
    assert attrs["http.status_code"] == 200
    assert "error" not in attrs


@otel_required
def test_tool_call_span_http_error(monkeypatch):
    provider, exporter = _provider_and_exporter()
    _install_test_tracer(monkeypatch, provider)
    result = _mock_executor(
        lambda r: httpx.Response(404, json={"detail": "snapshot not found"})
    ).execute("flights", {"as_of_snapshot_id": 999})
    assert result.ok is False
    spans = [sp for sp in exporter.get_finished_spans() if sp.name == "rtdp.agent.tool_call"]
    assert len(spans) == 1
    attrs = dict(spans[0].attributes)
    assert attrs["name"] == "flights"
    assert attrs["ok"] is False
    assert attrs["http.status_code"] == 404
    assert "HTTP 404" in attrs["error"]


@otel_required
def test_tool_call_span_unknown_tool_has_no_status(monkeypatch):
    provider, exporter = _provider_and_exporter()
    _install_test_tracer(monkeypatch, provider)
    result = _mock_executor(lambda r: httpx.Response(200)).execute("delete_everything", {})
    assert result.ok is False
    spans = [sp for sp in exporter.get_finished_spans() if sp.name == "rtdp.agent.tool_call"]
    assert len(spans) == 1
    attrs = dict(spans[0].attributes)
    assert attrs["ok"] is False
    assert "http.status_code" not in attrs  # transport was never hit
    assert attrs["error"]  # non-empty error string


# --- span teardown is instrumentation-safe: an __exit__ error never escapes or masks the body ---


class _ExitBoomCM:
    """A span context manager whose __enter__ succeeds but whose __exit__ always raises."""

    def __enter__(self):
        return object()  # a span-like object; span() wraps it in _SafeSpan

    def __exit__(self, *exc):
        raise RuntimeError("teardown boom")


class _ExitBoomTracer:
    def start_as_current_span(self, name):
        return _ExitBoomCM()


@otel_required
def test_span_swallows_exit_error_on_normal_completion(monkeypatch):
    # Body completes normally but span teardown raises: span() must swallow it, so nothing escapes.
    monkeypatch.setattr(telemetry, "_tracing_ready", True)
    monkeypatch.setattr("opentelemetry.trace.get_tracer", lambda *a, **k: _ExitBoomTracer())
    ran = []
    with telemetry.span("rtdp.exit_boom"):  # must NOT raise despite __exit__ boom
        ran.append(True)
    assert ran == [True]


@otel_required
def test_span_preserves_body_exception_when_exit_also_raises(monkeypatch):
    # Body raises ValueError AND teardown raises RuntimeError: the caller must still receive the
    # original ValueError, never the instrumentation exception.
    monkeypatch.setattr(telemetry, "_tracing_ready", True)
    monkeypatch.setattr("opentelemetry.trace.get_tracer", lambda *a, **k: _ExitBoomTracer())
    with pytest.raises(ValueError, match="body boom"):
        with telemetry.span("rtdp.both_boom"):
            raise ValueError("body boom")


# --- direct spans-on vs spans-off equivalence (isolated catalogs, identical inputs) ---


def _isolated_settings(tmp_path, name):
    """A fresh file:// Settings on its own warehouse/catalog under ``tmp_path`` (per-run)."""
    from rtdp.config import Settings

    root = tmp_path / name
    return Settings(
        _env_file=None,
        storage_backend="file",
        local_warehouse_dir=root / "warehouse",
        catalog_db_path=root / "warehouse" / "catalog.db",
        namespace="bronze",
        table_name="opensky_state_vectors",
    )


def _written_rows(settings):
    """All appended rows minus ``ingest_batch_id`` (the only non-deterministic column)."""
    table = build_catalog(settings).load_table(settings.table_identifier)
    rows = table.scan().to_arrow().to_pylist()
    for row in rows:
        row.pop("ingest_batch_id", None)
    return sorted(rows, key=repr)  # scan-order-independent comparison


@otel_required
def test_ingest_result_equivalent_spans_on_vs_off(tmp_path, monkeypatch):
    from datetime import UTC, datetime

    ingest_time = datetime(2026, 1, 1, tzinfo=UTC)

    def _run(settings):
        return run_ingest(
            settings,
            SyntheticSource(n_rows=12, seed=7, inject_warnings=False),
            ingest_time=ingest_time,
        )

    # Run once with tracing OFF...
    monkeypatch.setattr(telemetry, "_tracing_ready", False)
    off_settings = _isolated_settings(tmp_path, "off")
    off = _run(off_settings)

    # ...and once through the enabled in-memory tracer, identical deterministic inputs.
    provider, exporter = _provider_and_exporter()
    _install_test_tracer(monkeypatch, provider)
    on_settings = _isolated_settings(tmp_path, "on")
    on = _run(on_settings)

    # Behavior-relevant IngestResult fields identical (snapshot_id not compared across catalogs).
    assert (off.rows_in, off.rows_written, off.snapshot_count, off.dq.ok) == (
        on.rows_in, on.rows_written, on.snapshot_count, on.dq.ok,
    )
    assert off.dq.ok is True and on.dq.ok is True
    assert off.snapshot_id is not None and on.snapshot_id is not None
    # Written table rows identical.
    assert _written_rows(off_settings) == _written_rows(on_settings)
    # Sanity: the enabled run genuinely exercised the on-path (exactly one ingest span).
    ingest_spans = [sp for sp in exporter.get_finished_spans() if sp.name == "rtdp.ingest.batch"]
    assert len(ingest_spans) == 1


@otel_required
def test_tool_result_equivalent_spans_on_vs_off(monkeypatch):
    # Same tool call with tracing OFF then ON: the returned ToolResult must be equal.
    monkeypatch.setattr(telemetry, "_tracing_ready", False)
    off = _mock_executor(_ok_flights_response).execute("flights", {"icao24": "abc"})

    provider, exporter = _provider_and_exporter()
    _install_test_tracer(monkeypatch, provider)
    on = _mock_executor(_ok_flights_response).execute("flights", {"icao24": "abc"})

    assert off == on  # dataclass equality over every field
    assert [sp.name for sp in exporter.get_finished_spans()] == ["rtdp.agent.tool_call"]
