"""API tool layer for the Stage D agent.

The agent reaches data ONLY through the Stage 2A HTTP API — these tools are that boundary. Tool
definitions are derived from the live OpenAPI schema where practical (:func:`load_openapi` +
:func:`build_registry_from_openapi`), with a hand-curated :func:`static_registry` fallback for when
the schema cannot be fetched (and for offline tests). Only safe GET read endpoints are exposed
(:data:`READ_PATH_ALLOWLIST`); there is deliberately no write/mutation tool, which is what makes
the agent structurally incapable of changing anything.

A composite :data:`DIAGNOSE_TOOL_NAME` tool performs read-only DQ diagnosis by calling the read API
and re-deriving anomalies in code (see :mod:`rtdp.agent.dq`); it never touches the catalog or query
layer directly.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from . import dq

if TYPE_CHECKING:
    import httpx

READ_PATH_ALLOWLIST: tuple[str, ...] = (
    "/health",
    "/flights",
    "/flights/bbox",
    "/stats/flights-per-interval",
    "/snapshots",
    "/meta",
)

DIAGNOSE_TOOL_NAME = "diagnose_data_quality"
_FLIGHTS_PATH = "/flights"


@dataclass
class ApiTool:
    """One read-only tool the agent may call: a GET endpoint, or the composite diagnose tool."""

    name: str
    method: str  # always "GET" for Stage D
    path: str  # API path, or "" for the agent-side composite diagnose tool
    description: str
    param_types: dict[str, str] = field(default_factory=dict)  # name -> JSON-schema type
    required: tuple[str, ...] = ()
    param_descriptions: dict[str, str] = field(default_factory=dict)

    def to_openai(self) -> dict:
        properties: dict[str, dict] = {}
        for name, ptype in self.param_types.items():
            prop: dict = {"type": ptype}
            if name in self.param_descriptions:
                prop["description"] = self.param_descriptions[name]
            properties[name] = prop
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": list(self.required),
                },
            },
        }


@dataclass
class ToolResult:
    """The normalized outcome of a tool call, preserving endpoint + snapshot provenance."""

    tool_name: str
    endpoint: str  # e.g. "GET /flights" or "agent:diagnose_data_quality"
    params: dict
    ok: bool
    snapshot_id: int | None = None
    status_code: int | None = None
    data: Any = None
    error: str | None = None

    def to_content(self) -> str:
        payload: dict = {"endpoint": self.endpoint, "ok": self.ok, "snapshot_id": self.snapshot_id}
        if self.error is not None:
            payload["error"] = self.error
        else:
            payload["data"] = self.data
        return json.dumps(payload, default=str)


class ToolRegistry:
    """Name -> :class:`ApiTool` lookup plus OpenAI tool-definition export."""

    def __init__(self, tools: list[ApiTool]) -> None:
        self._tools: dict[str, ApiTool] = {t.name: t for t in tools}

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def get(self, name: str) -> ApiTool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def definitions(self) -> list[dict]:
        return [t.to_openai() for t in self._tools.values()]


# ----------------------------------------------------------------- OpenAPI -> tools


def load_openapi(base_url: str, client: httpx.Client) -> dict:
    """Fetch the live OpenAPI schema from the read API."""
    resp = client.get(f"{base_url.rstrip('/')}/openapi.json")
    resp.raise_for_status()
    return resp.json()


def _schema_type(schema: dict) -> str:
    """Best-effort JSON-schema type, unwrapping ``anyOf``/``oneOf`` and enums (FastAPI shapes)."""
    if not isinstance(schema, dict):
        return "string"
    if "type" in schema:
        return schema["type"]
    if "enum" in schema:
        return "string"
    for sub in schema.get("anyOf", []) + schema.get("oneOf", []):
        sub_type = sub.get("type")
        if sub_type and sub_type != "null":
            return sub_type
    return "string"


def _tool_name(path: str) -> str:
    """`/stats/flights-per-interval` -> `stats_flights_per_interval`."""
    return path.strip("/").replace("/", "_").replace("-", "_") or "root"


def build_registry_from_openapi(
    spec: dict,
    *,
    allowlist: tuple[str, ...] = READ_PATH_ALLOWLIST,
    include_diagnose: bool = True,
) -> ToolRegistry:
    """Transform allowlisted GET operations in an OpenAPI spec into a :class:`ToolRegistry`."""
    tools: list[ApiTool] = []
    paths = spec.get("paths", {})
    for path in allowlist:
        op = (paths.get(path) or {}).get("get")
        if not op:
            continue
        param_types: dict[str, str] = {}
        param_descriptions: dict[str, str] = {}
        required: list[str] = []
        for param in op.get("parameters", []):
            if param.get("in") != "query":
                continue
            name = param["name"]
            param_types[name] = _schema_type(param.get("schema", {}))
            if param.get("description"):
                param_descriptions[name] = param["description"]
            if param.get("required"):
                required.append(name)
        tools.append(
            ApiTool(
                name=_tool_name(path),
                method="GET",
                path=path,
                description=(op.get("summary") or op.get("description") or path).strip(),
                param_types=param_types,
                required=tuple(required),
                param_descriptions=param_descriptions,
            )
        )
    if include_diagnose:
        tools.append(_diagnose_tool())
    return ToolRegistry(tools)


def _diagnose_tool() -> ApiTool:
    return ApiTool(
        name=DIAGNOSE_TOOL_NAME,
        method="GET",
        path="",
        description=(
            "Read-only data-quality diagnosis. Fetches recent flight rows from the read API and "
            "re-derives anomalies (over-speed, unknown position_source, out-of-range altitude/"
            "coordinates, null required fields, duplicate state keys), then proposes remediations "
            "WITHOUT applying them. Bounded by the queried window and row limit."
        ),
        param_types={
            "start": "string",
            "end": "string",
            "icao24": "string",
            "sample_limit": "integer",
            "as_of_snapshot_id": "integer",
        },
        param_descriptions={
            "start": "Inclusive UTC lower bound on event_time (ISO-8601).",
            "end": "Inclusive UTC upper bound on event_time (ISO-8601).",
            "icao24": "Restrict to a single aircraft id.",
            "sample_limit": "Maximum rows to sample (bounded by the API max limit).",
            "as_of_snapshot_id": "Diagnose a specific snapshot via time-travel.",
        },
    )


def static_registry(*, include_diagnose: bool = True) -> ToolRegistry:
    """Hand-curated fallback mirroring the Stage 2A read endpoints (offline/fallback path)."""
    tools = [
        ApiTool("health", "GET", "/health", "Liveness and current snapshot id."),
        ApiTool(
            "flights", "GET", "/flights",
            "Typed flight/state-vector reads with optional filters and time-travel.",
            param_types={
                "icao24": "string", "callsign": "string", "start": "string", "end": "string",
                "limit": "integer", "offset": "integer", "as_of_snapshot_id": "integer",
                "as_of_timestamp": "string",
            },
        ),
        ApiTool(
            "flights_bbox", "GET", "/flights/bbox",
            "Flights within a lat/lon bounding box (min/max lat and lon are required).",
            param_types={
                "min_lat": "number", "max_lat": "number", "min_lon": "number", "max_lon": "number",
                "start": "string", "end": "string", "limit": "integer", "offset": "integer",
                "as_of_snapshot_id": "integer", "as_of_timestamp": "string",
            },
            required=("min_lat", "max_lat", "min_lon", "max_lon"),
        ),
        ApiTool(
            "stats_flights_per_interval", "GET", "/stats/flights-per-interval",
            "Counts of state vectors per hour or day, optionally grouped by origin_country.",
            param_types={
                "interval": "string", "start": "string", "end": "string", "group_by": "string",
                "as_of_snapshot_id": "integer", "as_of_timestamp": "string",
            },
        ),
        ApiTool("snapshots", "GET", "/snapshots", "Snapshot/provenance history, oldest first."),
        ApiTool("meta", "GET", "/meta", "Table metadata: snapshots, schema, partition spec."),
    ]
    if include_diagnose:
        tools.append(_diagnose_tool())
    return ToolRegistry(tools)


# ----------------------------------------------------------------- execution


def _drop_none(params: dict) -> dict:
    return {k: v for k, v in params.items() if v is not None}


def _http_error(status: int, data: Any) -> str:
    detail = data.get("detail") if isinstance(data, dict) else None
    return f"HTTP {status}" + (f": {detail}" if detail else "")


def _coerce(value: Any, json_type: str) -> Any:
    if json_type == "integer":
        return int(value)
    if json_type == "number":
        return float(value)
    if json_type == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes")
        return bool(value)
    return str(value)


def _validate_args(tool: ApiTool, arguments: dict) -> tuple[bool, dict, str | None]:
    """Check required params and coerce types. Unknown extras are ignored; errors are collected."""
    cleaned: dict = {}
    errors: list[str] = []
    for name in tool.required:
        if arguments.get(name) is None:
            errors.append(f"missing required parameter '{name}'")
    for name, value in arguments.items():
        if name not in tool.param_types or value is None:
            continue
        try:
            cleaned[name] = _coerce(value, tool.param_types[name])
        except (ValueError, TypeError):
            errors.append(f"parameter '{name}' must be {tool.param_types[name]}")
    if errors:
        return False, {}, "; ".join(errors)
    return True, cleaned, None


class ApiToolExecutor:
    """Runs tool calls as read-only HTTP GETs against the read API and normalizes results."""

    def __init__(
        self,
        base_url: str,
        client: httpx.Client,
        registry: ToolRegistry,
        *,
        max_rows: int = 1000,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client
        self._registry = registry
        self._max_rows = max_rows

    def execute(self, name: str, arguments: dict) -> ToolResult:
        tool = self._registry.get(name)
        if tool is None:
            return ToolResult(
                name, f"unknown:{name}", arguments, ok=False,
                error=f"unknown tool '{name}'; available: {self._registry.names()}",
            )
        endpoint = f"GET {tool.path}" if tool.path else f"agent:{name}"
        valid, cleaned, err = _validate_args(tool, arguments)
        if not valid:
            return ToolResult(name, endpoint, arguments, ok=False, error=err)
        if name == DIAGNOSE_TOOL_NAME:
            return self._diagnose(cleaned)
        return self._call_endpoint(tool, cleaned)

    def _get(self, path: str, params: dict) -> tuple[int, Any]:
        resp = self._client.get(f"{self._base_url}{path}", params=_drop_none(params))
        try:
            body = resp.json()
        except (json.JSONDecodeError, ValueError):
            body = {"raw": resp.text}
        return resp.status_code, body

    def _call_endpoint(self, tool: ApiTool, params: dict) -> ToolResult:
        endpoint = f"GET {tool.path}"
        try:
            status, data = self._get(tool.path, params)
        except Exception as exc:  # surface transport errors to the model/CLI
            return ToolResult(tool.name, endpoint, params, ok=False, error=f"request failed: {exc}")
        ok = 200 <= status < 300
        snapshot_id = data.get("snapshot_id") if isinstance(data, dict) else None
        return ToolResult(
            tool.name, endpoint, params, ok=ok, status_code=status, snapshot_id=snapshot_id,
            data=data if ok else None, error=None if ok else _http_error(status, data),
        )

    def _diagnose(self, params: dict) -> ToolResult:
        endpoint = f"agent:{DIAGNOSE_TOOL_NAME}"
        flight_params = {
            k: params[k] for k in ("start", "end", "icao24", "as_of_snapshot_id") if k in params
        }
        sample_limit = int(params.get("sample_limit") or self._max_rows)
        flight_params["limit"] = min(sample_limit, self._max_rows)
        try:
            status, data = self._get(_FLIGHTS_PATH, flight_params)
        except Exception as exc:  # surface transport errors to the model/CLI
            return ToolResult(
                DIAGNOSE_TOOL_NAME, endpoint, params, ok=False, error=f"request failed: {exc}"
            )
        if not (200 <= status < 300) or not isinstance(data, dict):
            return ToolResult(
                DIAGNOSE_TOOL_NAME, endpoint, params, ok=False, status_code=status,
                error=_http_error(status, data),
            )
        rows = data.get("items", [])
        snapshot_id = data.get("snapshot_id")
        diagnosis = dq.diagnose(
            rows, snapshot_id=snapshot_id, endpoint=f"GET {_FLIGHTS_PATH}",
            returned=data.get("count", len(rows)), requested_limit=flight_params["limit"],
        )
        return ToolResult(
            DIAGNOSE_TOOL_NAME, endpoint, params, ok=True, status_code=status,
            snapshot_id=snapshot_id, data=diagnosis,
        )
