"""Telemetry boundary — structured logging plus optional OpenTelemetry.

This is the single seam through which the platform emits observability signals. By default it
is a **no-op** for tracing: importing this module and running with ``RTDP_OTEL_ENABLED`` unset
pulls in **no** opentelemetry packages, so the default install stays dependency-free and
CI/tests run without the optional ``[otel]`` extra.

When ``RTDP_OTEL_ENABLED`` is true *and* the extra is installed, :func:`init_telemetry` /
:func:`instrument_fastapi` wire the OpenTelemetry SDK with an OTLP exporter (provider-agnostic;
point ``RTDP_OTEL_EXPORTER_OTLP_ENDPOINT`` at a collector or the Datadog Agent). If the extra
is missing while enabled, the boundary logs a warning and degrades to no-op rather than
failing — the app always boots. Nothing is exported until spans are actually produced.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

from .config import LogFormat

if TYPE_CHECKING:
    from fastapi import FastAPI

    from .config import Settings

_LOGGER_NAME = "rtdp"
_OUR_HANDLER_FLAG = "_rtdp_handler"
_tracing_ready = False


def get_logger(name: str | None = None) -> logging.Logger:
    """Return the ``rtdp`` logger (or a named child)."""
    return logging.getLogger(name or _LOGGER_NAME)


class _JsonFormatter(logging.Formatter):
    """Minimal JSON log formatter — stdlib only, no new dependency."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(settings: Settings) -> logging.Logger:
    """Configure the ``rtdp`` logger (text or JSON, level) from settings. Idempotent.

    Only the ``rtdp`` logger tree is touched (``propagate=False``), so uvicorn's and the root
    logger's configuration are left alone.
    """
    logger = get_logger()
    logger.setLevel(logging.getLevelNamesMapping().get(settings.log_level.upper(), logging.INFO))
    for handler in [h for h in logger.handlers if getattr(h, _OUR_HANDLER_FLAG, False)]:
        logger.removeHandler(handler)
    handler = logging.StreamHandler()
    setattr(handler, _OUR_HANDLER_FLAG, True)
    if settings.log_format is LogFormat.JSON:
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.propagate = False
    return logger


def _setup_tracing(settings: Settings) -> bool:
    """Set the global OpenTelemetry tracer provider with an OTLP exporter. Idempotent.

    Returns ``False`` (no-op) when the optional ``[otel]`` extra is not installed.
    """
    global _tracing_ready
    if _tracing_ready:
        return True
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        get_logger(__name__).warning(
            "RTDP_OTEL_ENABLED is set but the optional [otel] extra is not installed; "
            "telemetry stays no-op (install with `uv sync --extra otel`)."
        )
        return False
    provider = TracerProvider(
        resource=Resource.create({"service.name": settings.otel_service_name})
    )
    endpoint = settings.otel_exporter_otlp_endpoint
    exporter = OTLPSpanExporter(endpoint=endpoint) if endpoint else OTLPSpanExporter()
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    _tracing_ready = True
    get_logger(__name__).info(
        "OpenTelemetry tracing initialized (service=%s)", settings.otel_service_name
    )
    return True


def init_telemetry(settings: Settings) -> bool:
    """Configure logging and, when enabled+available, the OpenTelemetry tracer provider.

    Returns ``True`` when OTel tracing was initialized, ``False`` on the default/no-op path.
    Never raises on a missing extra. Intended to be called once at process start.
    """
    configure_logging(settings)
    if not settings.otel_enabled:
        return False
    return _setup_tracing(settings)


def instrument_fastapi(app: FastAPI, settings: Settings) -> bool:
    """Instrument a FastAPI app with OpenTelemetry when enabled+available; else no-op.

    Returns ``True`` if instrumentation was applied, ``False`` otherwise (disabled or the
    extra is not installed).
    """
    if not settings.otel_enabled:
        return False
    if not _setup_tracing(settings):
        return False
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    except ImportError:
        get_logger(__name__).warning(
            "RTDP_OTEL_ENABLED is set but opentelemetry-instrumentation-fastapi is not "
            "installed; FastAPI instrumentation skipped."
        )
        return False
    FastAPIInstrumentor.instrument_app(app)
    return True


# --- custom spans -------------------------------------------------------------------------
# A tiny span seam so ingestion/agent code can emit OpenTelemetry spans through this boundary
# without any `if otel_enabled` at the call site and without importing opentelemetry when the
# boundary is off. OTel attribute values must be scalar; anything else (or None) is skipped.
_SCALAR_ATTR_TYPES = (str, bool, int, float)


class _NoopSpan:
    """Do-nothing span for the disabled/fallback path — imports nothing from opentelemetry."""

    def set_attribute(self, key: str, value: object) -> None:
        return None


_NOOP_SPAN = _NoopSpan()


class _SafeSpan:
    """Wraps a real OTel span so ``set_attribute`` never raises into caller code.

    Skips ``None`` and non-scalar values and swallows+logs any error from the underlying span —
    instrumentation must never change the wrapped code's behavior.
    """

    def __init__(self, otel_span: object) -> None:
        self._span = otel_span

    def set_attribute(self, key: str, value: object) -> None:
        if value is None or not isinstance(value, _SCALAR_ATTR_TYPES):
            return
        try:
            self._span.set_attribute(key, value)
        except Exception:
            get_logger(__name__).debug("failed to set span attribute %r", key, exc_info=True)


@contextmanager
def span(name: str, **attrs: object) -> Iterator[_NoopSpan | _SafeSpan]:
    """Emit an OpenTelemetry span named ``name`` when tracing is ready; a no-op otherwise.

    Callers wrap code unconditionally::

        with span("rtdp.ingest.batch", rows_in=n) as s:
            s.set_attribute("rows_written", n)   # late attribute, known only at the end
            ...                                   # the wrapped work

    The yielded object always exposes ``set_attribute(key, value)`` (``None``/non-scalar values
    are skipped). When tracing is off this is a one-boolean-check no-op that imports **nothing**
    from opentelemetry. Instrumentation errors (span setup, attribute, or teardown) are swallowed
    and logged; exceptions raised by the wrapped body propagate unchanged — even if the span
    teardown also fails, the caller still receives the original body exception.
    """
    if not _tracing_ready:
        yield _NOOP_SPAN
        return
    try:
        from opentelemetry import trace

        cm = trace.get_tracer(_LOGGER_NAME).start_as_current_span(name)
        otel_span = cm.__enter__()  # actually starts the span (sampler/processors run here)
    except Exception:
        get_logger(__name__).warning(
            "failed to start span %r; continuing without tracing", name, exc_info=True
        )
        yield _NOOP_SPAN
        return
    # Span started cleanly. No `except` around the yield: a wrapped-body exception propagates
    # unchanged and, via sys.exc_info() in the finally, is handed to cm.__exit__ so the span can
    # record it. The finally's own try/except swallows+logs any OTel teardown/processor error so it
    # neither escapes nor replaces the body's exception (which keeps propagating from the try).
    try:
        safe = _SafeSpan(otel_span)
        for key, value in attrs.items():
            safe.set_attribute(key, value)
        yield safe
    finally:
        try:
            cm.__exit__(*sys.exc_info())
        except Exception:
            get_logger(__name__).debug(
                "error ending span %r; ignoring instrumentation failure", name, exc_info=True
            )
