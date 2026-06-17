.PHONY: setup info ingest demos test test-all lint fmt localstack-up localstack-down clean

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

clean:
	rm -rf _warehouse .localstack .pytest_cache .ruff_cache .coverage
