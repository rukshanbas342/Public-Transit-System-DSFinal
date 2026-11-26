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
## Run the project

Install and start [Docker Desktop](https://www.docker.com/products/docker-desktop/), then run:

```bash
git clone https://github.com/Salum17/distributed-transit-simulation.git
cd distributed-transit-simulation
docker compose up --build
```

1. Wait for the services to start. The first build downloads images and packages and may take a few minutes.
2. Open the [operator dashboard](http://localhost:8000) and sign in with username `operator` and password `operator`.
3. Watch the simulation and review suggested changes in the **Recommendations** panel. The clock starts at 05:00 and advances one simulated minute per real second. A full service day takes about 18 minutes at the default speed.

The dashboard refreshes every 1.5 seconds and shows bus locations, occupancy, passenger waits, route performance, worker count, and queue backlog.

To stop the stack, press **Ctrl+C**, then run `docker compose down`. To start with a clean database and queues, run `docker compose down -v`, which deletes the saved simulation data.

## Architecture

1. The **SimPy simulator** publishes vehicle and passenger events to RabbitMQ using publisher confirmations.
2. **Python workers** consume events and save updates in PostgreSQL. They acknowledge a message after the database transaction commits and track event IDs to avoid applying the same update twice.
3. The **scheduler** reads demand and service data from PostgreSQL and records proposed schedule changes.
4. The **React dashboard** displays monitoring data and recommendations from **FastAPI**. FastAPI records operator decisions and saves approved schedule changes. The simulator checks for newer schedule versions and reports when it applies a change.

## Demo scenarios

### Rush hour on Route A

From the repository folder, run:

```bash
SIM_SCENARIO=rush_hour SIM_SPEED=0.25 docker compose up --build
```

A full service day takes about 4.5 minutes. In the dashboard, review and approve a recommendation, then watch for it to be applied.

For the same scenario in Windows PowerShell, run:

```powershell
$env:SIM_SCENARIO = "rush_hour"
$env:SIM_SPEED = "0.25"
docker compose up --build
```

### Worker demos

| What to demonstrate | How to run it |
| --- | --- |
| Multiple workers | Start with `docker compose up --build --scale worker=3` and check the worker count in the dashboard. |
| Recovery after a worker stops | While the simulation is running, use a second terminal to run `docker compose stop worker`. Watch the backlog grow, then run `docker compose start worker` and watch it drain. |
| Scheduler approves its own suggestions | `AUTO_APPROVE=true docker compose up --build` |

For direct inspection, open the [FastAPI docs](http://localhost:8000/docs) or the [RabbitMQ management UI](http://localhost:15672). Sign into RabbitMQ with username `transit` and password `transit`, then look for `transit.events.process` under **Queues**.

## Repository layout

| Path | Contents |
| --- | --- |
| `config/` | Simulation and scheduler settings, and scenario files |
| `transit/common/` | Shared configuration, event schemas, database models, and RabbitMQ helpers |
| `transit/simulator/` | Bus network and passenger simulation |
| `transit/worker/` | Event handling and database updates |
| `transit/scheduler/` | Demand analysis and schedule recommendations |
| `transit/api/` and `dashboard/` | FastAPI backend and React dashboard |
| `migrations/` and `transit/db/` | Database schema migrations and initial data |
| `docker/` and `docker-compose.yml` | Container build and deployment configuration |
| `tests/` | Automated tests |
