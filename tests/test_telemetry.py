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

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rtdp import telemetry
from rtdp.api import create_app
from rtdp.config import LogFormat

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
