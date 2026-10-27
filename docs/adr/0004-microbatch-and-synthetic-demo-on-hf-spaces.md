# 4. Micro-batch ingestion and a synthetic-only demo on Hugging Face Spaces

Status: Accepted

## Context

Ingestion is a scheduled loop that polls a source and appends one micro-batch per interval through
the existing writer. It is near-real-time micro-batch ingestion, not true streaming, and Kafka or
Flink-style streaming was kept out of scope.

The public demo cannot show real OpenSky data. Its license is non-commercial and does not allow
redistribution, so the committed and CI-tested data path is synthetic and real ingestion stays
opt-in, network-gated, never run in CI, never committed.

The first public demo ran on one rented virtual machine, with Caddy for HTTPS and a Datadog agent.
That host is retired, so nothing is deployed from that path now. According to the owner's check on
2026-10-26, the Hugging Face Docker SDK is a paid feature and the only free hardware offered for a
new Space was ZeroGPU.

## Decision

- Keep scheduled micro-batch ingestion as the ingestion model.
- Make the public demo a Hugging Face Space on the free Gradio SDK, with synthetic data only. At
  startup it ingests seeded synthetic batches into a throwaway `file://` warehouse and calls the
  real read API in-process, so the routes are the real ones and no network or server is needed.
  The agent is not exposed and the Space holds no key.
- Deploy it from CI with a gated job that pushes `deploy/hf-space/` to the Space with plain git, no
  third-party action, using a token scoped to that one Space. The job fills the commit placeholder
  in `requirements.txt` with the commit it deploys.
- Pin pyiceberg in the Space's requirements to the version in `uv.lock`, because a Space build does
  not use the lockfile. A test keeps the two equal.
- Carry one never-called `@spaces.GPU` function so the app passes a ZeroGPU startup check. Nothing
  in the app uses a GPU.
- Keep the VM deployment files as a portable path, relabelled as such, with its host retired.

## Consequences

- The demo's state is rebuilt on every start, and the `file://` backend is a dev/CI convenience
  only — not the target architecture.
- The demo shows the read API and time travel, not ingestion over time and not the agent.
- A merge to `main` triggers a deploy only after a reviewer approves the `production`
  environment. The job cannot confirm that the Space came up, so the Space's `/health` has to be
  checked by hand before a URL is advertised.
- The ZeroGPU stub rests on community reports that a Space without such a function fails its
  startup check. The Hugging Face documentation does not say either way.
