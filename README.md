# Public Transit System: Distributed Simulation & Adaptive Scheduling

SOFE 4790U Distributed Systems, Final Project (Group 8)

A simulated bus network that publishes a stream of vehicle and passenger events
through RabbitMQ. Workers process the events into PostgreSQL, a scheduling service
recommends departure intervals based on demand, and an operator approves the
recommendations on a dashboard. The simulator then applies approved changes at
the next terminal departure.

```
                        ┌──────────── approved schedule (route_schedules) ─────────────┐
                        ▼                                                               │
┌─────────────────┐  events   ┌───────────┐      ┌────────────┐      ┌────────────┐    │
│ Simulator       │──────────▶│ RabbitMQ  │─────▶│ Worker × N │─────▶│ PostgreSQL │◀───┤
│ (SimPy)         │ publish + │ durable   │ ack  │ idempotent │commit│            │    │
└─────────────────┘ confirms  └───────────┘ after└────────────┘      └─────┬──────┘    │
                                            commit                         │           │
                                         ┌──────────────┐  recommendations │           │
                                         │  Scheduler   │◀─────────────────┤           │
                                         └──────────────┘                  ▼           │
                                         ┌──────────────────────────────────────────┐  │
                                         │ FastAPI + React dashboard (operator)     │──┘
                                         └──────────────────────────────────────────┘
```

## For evaluators: run it in 3 steps

You only need **[Docker Desktop](https://www.docker.com/products/docker-desktop/)**
(Windows, macOS or Linux), running. No Python, Node or database install is needed.

```bash
git clone https://github.com/Salum17/distributed-transit-simulation.git
cd distributed-transit-simulation
docker compose up --build
```

1. **Wait for the build.** The first run downloads images and packages (a few minutes, needs
   internet). Later runs start in seconds. Leave this terminal open; it shows the logs.
2. **Open http://localhost:8000** and sign in as **`operator` / `operator`**.
3. **Watch the day run.** The simulated clock starts at 05:00 and moves 1 minute per real second
   (the full day takes about 18 minutes). Approve or decline the scheduler's suggestions in the
   Recommendations panel; approved changes take effect at the route's next departure.

To stop: **Ctrl+C**, then `docker compose down` (add `-v` to also delete the data).

| Try this | Command |
|---|---|
| Rush hour on Route A, whole day in about 4.5 minutes | `SIM_SCENARIO=rush_hour SIM_SPEED=0.25 docker compose up --build` |
| Scheduler approves its own suggestions | `AUTO_APPROVE=true docker compose up --build` |
| Three workers sharing the queue | `docker compose up --build --scale worker=3` |
| Worker failure: watch the dashboard, then recover | `docker compose stop worker`, then `docker compose start worker` |

On Windows PowerShell, set variables first instead: `$env:SIM_SCENARIO="rush_hour"; docker compose up --build`.

Only ports **8000** (dashboard) and **15672** (RabbitMQ UI) are used on your machine. If one is
taken, change it, for example `API_PORT=8080 docker compose up --build`. If something looks
wrong, `docker compose down -v` and start again gives a clean database.

## Status

| Component | Path | Status |
|---|---|---|
| Shared contracts (config, events, DB schema, messaging) | `transit/common/` | ✅ done |
| Migrations + seed | `migrations/`, `transit/db/` | ✅ done |
| Simulator | `transit/simulator/` | ✅ done, 21 tests |
| Deployment (Compose, Dockerfiles) | `docker-compose.yml`, `docker/` | ✅ one `docker compose up` starts everything |
| Event worker | `transit/worker/` | ✅ done |
| Scheduler | `transit/scheduler/` | ✅ done |
| API | `transit/api/` | ✅ done |
| Dashboard | `dashboard/` | ✅ done, http://localhost:8000 |
| Experiments | `transit/experiments/` | ⬜ to do |

Files marked "to do" exist with a short comment describing their purpose and
expected outcome. **[CONTEXT.md](CONTEXT.md)** has the full handoff: design,
contracts and what's left.

## Quick start

### Option A: whole stack with Docker (how evaluators run it)

Requires Docker Desktop (Compose v2, any version from 2023 onwards).

```bash
docker compose up --build
```

This starts PostgreSQL and RabbitMQ, runs migrations and seeding, then starts the
simulator, event worker, scheduler and the API with the dashboard. Then:

- **Operator dashboard:** http://localhost:8000 (login `operator` / `operator`, set by
  `OPERATOR_USERNAME` / `OPERATOR_PASSWORD`). Everything is on one page and refreshes every
  1.5 s: the simulated clock, passenger totals and waits, system health (worker count, queue
  backlog, dead letters, event rate, worker lag, simulator and scheduler status), a live map of
  every route, per-route and per-bus tables, and the scheduler's recommendations with
  Approve / Decline. You don't need the RabbitMQ UI or the terminal to follow a run.
- **API docs:** http://localhost:8000/docs (every endpoint, try-it-out included).
- **RabbitMQ UI:** http://localhost:15672 (login `transit` / `transit`). Under
  *Queues*, the `transit.events.process` queue is consumed by the worker.
- **Database:** `docker compose exec postgres psql -U transit` (or `make psql`). For example:
  `docker compose exec postgres psql -U transit -c "table route_schedules"`. Port 5432 isn't
  published by default, so a PostgreSQL already on your machine can't clash; developers who
  want it on the host use `make infra` / `docker-compose.dev.yml`.

Useful variations:

```bash
SIM_SCENARIO=rush_hour SIM_SPEED=0.25 docker compose up --build   # surge on Route A, 4 sim-min per second
docker compose up --build --scale worker=3                          # three competing workers
AUTO_APPROVE=true docker compose up --build                         # scheduler applies its own suggestions
docker compose down        # stop (keeps data)
docker compose down -v     # stop and wipe the database and queues
```

### Option B: simulator only, no Docker (fastest for development)

Requires Python 3.11+.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest -q                                                    # run the tests
python -m transit.simulator --scenario quick_test --sink none --schedule fixed   # prints run metrics
python -m transit.simulator --scenario rush_hour --sink jsonl --speed 0 --schedule fixed  # events -> out/events.jsonl
```

## Configuring test runs

Everything about the simulation lives in **`config/simulation.yaml`**: the network,
fleet, demand curve and disruptions. Scheduler parameters live in
**`config/scheduler.yaml`**. Values are applied in layers, and later layers win:

| Layer | How | Example |
|---|---|---|
| Base file | edit `config/simulation.yaml` | change stop travel times |
| Scenario | `--scenario NAME` or `SIM_SCENARIO=NAME` | `rush_hour`, `breakdown`, `quick_test`, `baseline` |
| Any single value | `--set key.path=value` or `SIM_SET="a=1;b=2"` | `--set fleet.capacity=60` |
| Shortcuts | `--seed` / `SIM_SEED`, `--speed` / `SIM_SPEED` | `--seed 7 --speed 0` |

To create a new scenario, add a YAML file to `config/scenarios/` containing only
the keys you want to change. In Docker, `config/` is mounted into the containers,
so YAML edits take effect on the next run without rebuilding.

`--speed` is real seconds per simulated minute. The default of `1` plays a full
18-hour service day in about 18 minutes. `0` runs as fast as possible, which takes
under a second without a broker.

## Repository layout

```
config/            simulation + scheduler parameters, scenarios
transit/common/    shared contracts: config loader, event schema, DB models, RabbitMQ helpers
transit/db/        seed script
transit/simulator/ SimPy simulator
transit/worker/    event consumer -> PostgreSQL
transit/scheduler/ demand analysis + recommendations
transit/api/       FastAPI backend
transit/experiments/ headless fixed-vs-adaptive runs for the report
dashboard/         React + TypeScript operator UI
migrations/        Alembic migrations
docker/            Dockerfiles
tests/             pytest suite
```

## Demo script (target for the final presentation)

1. Run `docker compose up --build` with the baseline scenario and open http://localhost:8000. Show buses moving and queues in the dashboard.
2. Start a rush hour on Route A (`SIM_SCENARIO=rush_hour`). The scheduler recommends a shorter interval, the operator approves it, and the simulator applies it at the next departure.
3. Fault tolerance: `docker compose stop worker`. Show the backlog growing in the RabbitMQ UI, then `docker compose start worker` and show it catch up with no double counting.
4. Show the fixed-vs-adaptive results from `transit/experiments`.
