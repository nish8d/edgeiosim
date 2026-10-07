# edgeio

[![CI](https://github.com/nish8d/edgeiosim/actions/workflows/ci.yml/badge.svg)](https://github.com/nish8d/edgeiosim/actions/workflows/ci.yml)

A health monitoring platform for a fleet of edge devices. Fifty simulated devices report telemetry every five minutes over Kafka. A stream worker validates and stores the readings in TimescaleDB and raises alerts. A REST API and a React dashboard show live status, history and alerts.

```
Simulated devices ──JSON every 5 min──▶ Kafka (device.health)
                                            │
                                            ▼
                                  Stream worker: validate → transform → store → alert
                                            │                      │
                                            ▼                      ▼
                                  PostgreSQL + TimescaleDB    Kafka (device.health.dlq)
                                       │            │
                                       ▼            ▼
                                   REST API ──▶ Dashboard
```

The simulator stands in for a real `health.py` agent that each device would run over Tailscale. Because of this, the device → Kafka message contract is the most important interface in the repo. It's strict, versioned and doesn't depend on the simulator.

## Features

- **Realistic simulated fleet.** Device state carries over between readings: uptime and network counters only grow, disks fill slowly, and CPU follows a daily curve. Devices randomly enter fault scenarios: overheat, disk fill, memory leak, packet loss, service or container crash, going offline, and reboot. A small fraction of messages are deliberately malformed to exercise the dead-letter path.
- **Strict message contract.** Pydantic v2 models plus a generated JSON Schema (`contracts/health.schema.json`) for producers that aren't written in Python. `device_id` must be a Tailscale CGNAT address (`100.64.0.0/10`).
- **Reliable stream worker:**
  - At-least-once delivery with idempotent inserts. Offsets are committed only after the database transaction commits.
  - Invalid or poison messages go to a dead-letter topic and never block a partition.
  - Network rates are derived from counters, and a counter reset is treated as a reboot.
- **Rule-based alerts with hysteresis.** Rules cover CPU temperature, disk, RAM, packet loss, services, stopped containers and offline devices. A device has at most one open alert per rule.
- **TimescaleDB storage.** Readings go in a hypertable. Hourly and daily continuous aggregates keep long-range charts fast. Retention policies apply: raw data for 30 days, hourly rollups for 1 year.
- **Read-only FastAPI service.** OpenAPI docs are served at `/docs`. The resolution of metric history (raw, hourly or daily) is picked automatically from the time range. If the database is down, the API answers 503 instead of hanging.
- **Dashboard:**
  - Built with React, TypeScript, TanStack Query and Recharts.
  - Pages: fleet overview, per-device charts with a range picker, and alert history.
  - API types are generated from the OpenAPI schema.

## Quick start

Requirements: Docker with Compose v2.

```bash
git clone https://github.com/nish8d/edgeiosim.git
cd edgeiosim
make up
```

| Service | URL |
|---|---|
| Dashboard | http://localhost:5173 |
| API + OpenAPI docs | http://localhost:8000/docs |
| Kafka (host) | `localhost:9092` |
| TimescaleDB (host) | `localhost:5433` (user/db `edgeio`) |

Devices publish with random initial jitter at real-time intervals (every 5 minutes), so the fleet fills in over the first few minutes. Stop the stack with `make down`.

Ports can be overridden with `DASHBOARD_PORT` and `TIMESCALE_PORT`.

## Development

Requirements: [uv](https://docs.astral.sh/uv/), Python 3.12, Node 22, Docker (for integration tests).

```bash
uv sync                          # Python workspace + dev tools
npm --prefix dashboard ci        # dashboard dependencies
```

| Command | What it does |
|---|---|
| `make test` | Python unit tests + dashboard Vitest |
| `make test-int` | Integration tests against real Kafka and TimescaleDB (Testcontainers) |
| `make lint` | ruff, mypy `--strict`, eslint, prettier, tsc |
| `make fmt` | Format Python and TypeScript |
| `make dev-api` | API on :8000 with the local Python env |
| `make dev-dashboard` | Vite dev server on :5173, proxying `/api` to :8000 |
| `make logs s=worker` | Tail one service's logs |
| `make psql` | psql shell into TimescaleDB |
| `make topics` | Describe topics and consumer-group lag |
| `make schema` | Regenerate `contracts/health.schema.json` from the Pydantic model |
| `make openapi` | Regenerate `dashboard/openapi.json` and the TypeScript API types |

The dev servers bind the same ports as the Compose stack. To run them alongside it, stop the Compose `dashboard` and `api` services first, or set `DASHBOARD_PORT`.

## Repository layout

```
contracts/          Wire format: Pydantic models, JSON Schema, example payloads
simulator/          Virtual device fleet → Kafka
worker/             Kafka consumer: validate → transform → store → alert; DB migrator
api/                FastAPI read API over TimescaleDB
dashboard/          React + Vite + TypeScript dashboard (served by nginx in Compose)
db/migrations/      Ordered SQL migrations, applied at startup
tests/integration/  Cross-service tests with Testcontainers
docker/             Shared Python service Dockerfile
```

## The message contract

Each device publishes one JSON message per interval to `device.health`, keyed by `device_id`:

```json
{
  "schema_version": 1,
  "device_id": "100.101.12.7",
  "timestamp": "2026-10-06T09:45:00Z",
  "system": {
    "hostname": "edge-001", "os": "Ubuntu 24.04", "uptime_seconds": 382941,
    "cpu_usage_percent": 43.7, "cpu_temperature_c": 57.2, "load_1m": 1.42,
    "ram_total_mb": 16384, "ram_used_mb": 9271, "ram_usage_percent": 56.6
  },
  "disk": { "root_total_gb": 476, "root_used_gb": 291, "root_free_gb": 185, "root_usage_percent": 61.1 },
  "network": { "interface": "eth0", "rx_bytes": 482938192, "tx_bytes": 182938291, "packet_loss_percent": 0.0 },
  "services": { "docker": "running", "postgresql": "running", "edge_streamer": "running" },
  "containers": { "running": 5, "stopped": 1 }
}
```

The full validation rules are in [`CLAUDE.md`](CLAUDE.md#4-the-message-contract-devicehealth) and the schema is in [`contracts/health.schema.json`](contracts/health.schema.json). Messages that fail validation go to `device.health.dlq` with the original bytes. Headers on the DLQ record describe the error.

## API

All endpoints are under `/api/v1`:

| Endpoint | Description |
|---|---|
| `GET /healthz` | Liveness + database reachability |
| `GET /devices` | Devices with latest status and metrics (`status`, `limit`, `offset`) |
| `GET /devices/{device_id}` | Latest metrics, services, containers and open alerts |
| `GET /devices/{device_id}/metrics` | History for one metric (`metric`, `from`, `to`, `bucket`) |
| `GET /alerts` | Open / resolved alerts, newest first (`state`, `device_id`, `severity`) |
| `GET /fleet/summary` | Status counts, open alerts by severity, hottest / fullest-disk devices |

## CI

GitHub Actions ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs on every push to `main` and on pull requests. It has four jobs:

- **Python:** lint, mypy, unit tests, and a check that the generated schema and OpenAPI files are up to date.
- **Integration:** the integration tests, run with Testcontainers.
- **Dashboard:** lint, tests and the production build.
- **Docker:** builds every image in the Compose stack.

## Tech stack

Python 3.12 · uv · Pydantic v2 · confluent-kafka · psycopg 3 · FastAPI · Apache Kafka 3.8 (KRaft) · TimescaleDB 2.17 (PostgreSQL 16) · React 19 · Vite · TypeScript · TanStack Query · Recharts · Vitest · Docker Compose · GitHub Actions
