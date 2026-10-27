# 2. The agent and the MCP server are HTTP clients of the read API

Status: Accepted

## Context

The Stage D agent answers questions about the flight data and diagnoses data quality. It would be
simple to give it direct access to the query layer and the catalog. It must also be impossible for
it to change data, and the same data should be reachable from other clients, such as an MCP client
or a demo UI, without each one growing its own path into the lakehouse.

## Decision

- The agent and the MCP server reach data only through the Stage 2A HTTP API. They never import
  the query layer, the catalog, DuckDB, or the Iceberg table APIs.
- Tool definitions come from the live OpenAPI schema through an allowlist of read-only GET
  endpoints, with a static fallback. There is no write tool, so the agent is structurally unable to
  change anything.
- Boundary tests parse the imports of `src/rtdp/agent/` and `src/rtdp/mcp/` and fail if a data-plane
  package or an unapproved `rtdp` module appears.
- Data-quality diagnosis is re-derived in the agent from the rows the API returns. Pandera WARN and
  FAIL history is not persisted and there is no data-quality endpoint, and adding either would be a
  new feature.

## Consequences

- Any new capability for the agent or the MCP server needs an API endpoint first.
- Diagnosis is a bounded sample: one queried window, capped by the API row limit, over the
  snapshots queried. The agent says so in its answers.
- Each tool call is an HTTP request, which costs latency but gives one place to apply limits, time
  travel, and typed responses.
- The Hugging Face Space demo builds its own throwaway warehouse and the app, so it imports the
  ingest and app factories. Its UI still calls only the routes through an in-process client, and a
  test enforces that.
