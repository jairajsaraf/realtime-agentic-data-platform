"""Stage 2A FastAPI serving layer (thin transport over :mod:`rtdp.query`)."""

from __future__ import annotations

from .app import create_app

__all__ = ["create_app"]
