---
title: rtdp read API demo
sdk: gradio
sdk_version: "6.30.0"
python_version: "3.12.12"
app_file: app.py
pinned: false
short_description: Read-only Iceberg lakehouse API demo, synthetic data only
---

# rtdp read API demo

A read-only demo of the read API from
[realtime-agentic-data-platform](https://github.com/jairajsaraf/realtime-agentic-data-platform).
It runs on **synthetic data only**.

When the Space starts it ingests two seeded synthetic batches into a throwaway Apache Iceberg
table, builds the real FastAPI app, and calls it in-process (the same routes, no network). The
buttons call `GET /health`, `/livez`, `/snapshots`, `/stats/flights-per-interval` and `/flights`,
and show the endpoint next to the JSON it returned. The snapshot dropdown reads older snapshots
through the API's time-travel parameter.

- The agent is not exposed here: there is no model and no key.
- Nothing on this page writes data.
- The warehouse is a local `file://` path, a dev/CI convenience only, not the target
  architecture. It is rebuilt every time the Space starts.
- Ingestion in the main project is near-real-time micro-batch ingestion, not true streaming.

The Space is deployed from the `deploy/hf-space/` folder of the repository by CI. This README's
YAML block is the Space configuration: `python_version` is `3.12.12` because the Hugging Face
ZeroGPU documentation lists that as its supported 3.12 release. `app.py` carries one never-called
`@spaces.GPU` function so a ZeroGPU Space passes its startup check; nothing here uses a GPU.
