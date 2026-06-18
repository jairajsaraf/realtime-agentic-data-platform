.PHONY: setup info ingest demos test test-all lint fmt localstack-up localstack-down reset clean

# Common commands mirrored for Linux/CI. On Windows, run the `uv run ...` lines directly.

setup:
	uv sync

info:
	uv run rtdp info

ingest:
	uv run rtdp ingest

demos:
	uv run rtdp demo catalog

test:
	uv run pytest -m "not localstack"

test-all:
	uv run pytest

lint:
	uv run ruff check .

fmt:
	uv run ruff format .

localstack-up:
	docker compose up -d

localstack-down:
	docker compose down

# Clear local lakehouse state so the SQLite catalog can't point at S3 metadata
# that `docker compose down` already discarded (avoids FileNotFoundError on a
# second LocalStack run). Run after `localstack-down` before re-ingesting.
reset:
	rm -rf _warehouse _demo .localstack

clean:
	rm -rf _warehouse _demo .localstack .pytest_cache .ruff_cache .coverage
