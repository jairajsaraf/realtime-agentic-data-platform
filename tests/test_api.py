"""FastAPI TestClient tests against the file:// backend (no Docker)."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from rtdp.api import create_app
from rtdp.api.routes import _clamp_limit
from rtdp.ingest import run_ingest
from rtdp.sources.synthetic import SyntheticSource


def _seed(settings, n_rows, seed):
    return run_ingest(settings, SyntheticSource(n_rows=n_rows, seed=seed, inject_warnings=False))


@pytest.fixture
def client(file_settings):
    """Two snapshots (20 then 15 rows) so time-travel is exercisable. Yields (client, r1, r2)."""
    r1 = _seed(file_settings, 20, 1)
    time.sleep(0.05)
    r2 = _seed(file_settings, 15, 2)
    app = create_app(file_settings)
    with TestClient(app) as c:
        yield c, r1, r2


_STATE_VECTOR_FIELDS = {
    "icao24", "callsign", "origin_country", "event_time", "time_position", "last_contact",
    "longitude", "latitude", "baro_altitude", "geo_altitude", "on_ground", "velocity",
    "true_track", "vertical_rate", "squawk", "spi", "position_source", "category",
    "ingest_time", "ingest_batch_id", "source_name",
}


def test_health_ok(client):
    c, r1, r2 = client
    resp = c.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["catalog_reachable"] is True
    assert body["table_loadable"] is True
    assert body["current_snapshot_id"] == r2.snapshot_id


def test_meta(client):
    c, r1, r2 = client
    resp = c.get("/meta")
    assert resp.status_code == 200
    body = resp.json()
    assert body["table_identifier"].endswith("opensky_state_vectors")
    assert body["snapshot_count"] == 2
    assert len(body["schema"]) == 21  # serialized under the "schema" alias
    assert body["partition_spec"][0]["name"] == "event_day"
    assert body["partition_spec"][0]["transform"] == "day"


def test_snapshots(client):
    c, r1, r2 = client
    resp = c.get("/snapshots")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    assert {s["snapshot_id"] for s in body} == {r1.snapshot_id, r2.snapshot_id}
    assert all(s["operation"] == "append" for s in body)


def test_flights_schema_and_count(client):
    c, r1, r2 = client
    resp = c.get("/flights", params={"limit": 1000})
    assert resp.status_code == 200
    body = resp.json()
    assert body["snapshot_id"] == r2.snapshot_id
    assert body["count"] == 35
    assert set(body["items"][0].keys()) == _STATE_VECTOR_FIELDS


def test_flights_bbox(client):
    c, r1, r2 = client
    resp = c.get(
        "/flights/bbox",
        params={"min_lat": -90, "max_lat": 90, "min_lon": -180, "max_lon": 180, "limit": 1000},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] >= 1
    assert set(body["items"][0].keys()) == _STATE_VECTOR_FIELDS


def test_stats_per_interval(client):
    c, r1, r2 = client
    resp = c.get("/stats/flights-per-interval", params={"interval": "day"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["snapshot_id"] == r2.snapshot_id
    assert body["buckets"]
    assert {"bucket_start", "group", "count"} == set(body["buckets"][0].keys())


def test_stats_group_by_origin_country(client):
    c, r1, r2 = client
    resp = c.get(
        "/stats/flights-per-interval", params={"interval": "day", "group_by": "origin_country"}
    )
    assert resp.status_code == 200
    assert all(b["group"] is not None for b in resp.json()["buckets"])


def test_as_of_conflict_returns_400(client):
    c, r1, r2 = client
    resp = c.get(
        "/flights",
        params={"as_of_snapshot_id": r1.snapshot_id, "as_of_timestamp": "2026-06-14T00:00:00Z"},
    )
    assert resp.status_code == 400


def test_as_of_timestamp_before_history_returns_404(client):
    c, r1, r2 = client
    resp = c.get("/flights", params={"as_of_timestamp": "2000-01-01T00:00:00Z"})
    assert resp.status_code == 404


def test_validation_errors_return_422(client):
    c, r1, r2 = client
    # bad interval enum
    assert c.get("/stats/flights-per-interval", params={"interval": "week"}).status_code == 422
    # latitude out of range
    bad_bbox = {"min_lat": -999, "max_lat": 90, "min_lon": 0, "max_lon": 1}
    assert c.get("/flights/bbox", params=bad_bbox).status_code == 422
    # missing required bbox param
    assert c.get("/flights/bbox", params={"min_lat": 0}).status_code == 422


def test_time_travel_snapshot_id_vs_current(client):
    """The headline proof: as_of S1 sees only S1's rows; default sees the latest."""
    c, r1, r2 = client
    at_s1 = c.get("/flights", params={"as_of_snapshot_id": r1.snapshot_id, "limit": 1000}).json()
    current = c.get("/flights", params={"limit": 1000}).json()
    assert at_s1["count"] == 20
    assert at_s1["snapshot_id"] == r1.snapshot_id
    assert current["count"] == 35
    assert current["snapshot_id"] == r2.snapshot_id


def test_time_travel_by_timestamp(client):
    c, r1, r2 = client
    snaps = c.get("/snapshots").json()
    s1_ts = next(s["timestamp"] for s in snaps if s["snapshot_id"] == r1.snapshot_id)
    resp = c.get("/flights", params={"as_of_timestamp": s1_ts, "limit": 1000}).json()
    assert resp["count"] == 20
    assert resp["snapshot_id"] == r1.snapshot_id


def test_openapi_lists_all_endpoints(client):
    c, r1, r2 = client
    paths = set(c.get("/openapi.json").json()["paths"].keys())
    assert {
        "/health",
        "/flights",
        "/flights/bbox",
        "/stats/flights-per-interval",
        "/snapshots",
        "/meta",
    } <= paths


# --------------------------------------------------------------- limit clamping
def _limits(default, maximum):
    # _clamp_limit only reads these two attributes — avoid Settings/.env coupling.
    return SimpleNamespace(api_default_limit=default, api_max_limit=maximum)


def test_clamp_limit_default_under_max_uses_default():
    assert _clamp_limit(None, _limits(default=50, maximum=100)) == 50


def test_clamp_limit_default_over_max_is_capped():
    # A misconfigured default must not bypass the advertised cap.
    assert _clamp_limit(None, _limits(default=500, maximum=100)) == 100


def test_clamp_limit_explicit_is_capped_at_max():
    assert _clamp_limit(9999, _limits(default=100, maximum=100)) == 100


def test_clamp_limit_explicit_under_max_is_unchanged():
    assert _clamp_limit(25, _limits(default=100, maximum=100)) == 25
