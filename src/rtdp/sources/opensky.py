"""Live OpenSky REST source — the real public-dataset path (opt-in, network-gated).

Fetches /states/all. Uses OAuth2 client-credentials when client id/secret are set,
otherwise the anonymous snapshot tier. This source hits the network, so it is NEVER
run in CI and its output is NEVER committed (OpenSky's license forbids redistribution).
Use it via ``rtdp ingest --source opensky-live``.

The /states/all ``states`` array is positional; :meth:`state_to_dict` (pure, unit-tested)
maps it to the dict shape the transforms expect.
"""

from __future__ import annotations

from .base import RawBatch

# Positional layout of an OpenSky /states/all state vector.
_FIELDS = [
    "icao24",
    "callsign",
    "origin_country",
    "time_position",
    "last_contact",
    "longitude",
    "latitude",
    "baro_altitude",
    "on_ground",
    "velocity",
    "true_track",
    "vertical_rate",
    "sensors",  # dropped — not in the bronze schema
    "geo_altitude",
    "squawk",
    "spi",
    "position_source",
    "category",
]


class OpenSkyLiveSource:
    name = "opensky_live"

    def __init__(self, settings) -> None:
        self.settings = settings

    def fetch(self) -> RawBatch:
        import httpx

        s = self.settings
        headers: dict[str, str] = {}
        if s.opensky_client_id and s.opensky_client_secret:
            headers["Authorization"] = f"Bearer {self._token(httpx)}"

        resp = httpx.get(s.opensky_states_url, headers=headers, timeout=30.0)
        resp.raise_for_status()
        states = resp.json().get("states") or []
        records = [self.state_to_dict(state) for state in states]
        return RawBatch(records=records, source_name=self.name)

    def _token(self, httpx) -> str:
        s = self.settings
        resp = httpx.post(
            s.opensky_token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": s.opensky_client_id,
                "client_secret": s.opensky_client_secret,
            },
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.json()["access_token"]

    @staticmethod
    def state_to_dict(state: list) -> dict:
        """Map a positional OpenSky state vector to a named record (drops ``sensors``)."""
        rec = {name: (state[idx] if idx < len(state) else None) for idx, name in enumerate(_FIELDS)}
        rec.pop("sensors", None)
        return rec
