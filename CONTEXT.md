# CONTEXT.md: Project handoff

Read this before working on any component. It is written for teammates **and**
for AI coding assistants: paste it into your assistant's context, or point it at
this file. It explains what exists, the contracts you must not break, and what is
left to build.

---

## 1. What we are building (from the submitted proposal)

A distributed public transit system on one machine, started with `docker compose up`.

- **Problem:** fixed bus schedules with a fixed fleet cause crowding and long waits
  at peak times, and empty buses off-peak.
- **System:** a simulator produces events, RabbitMQ carries them, workers write
  them to PostgreSQL, a scheduler recommends departure intervals, and an operator
  approves them on the dashboard. The simulator then applies the approved schedule.
- **Research questions:**
  1. Does demand-based scheduling reduce passenger wait time with the same fleet?
  2. Does the system recover from component failures (such as a worker stopping)
     without losing or double-counting events?
- **Scope:** buses only (no trains), fictional stops, an **operator-only**
  dashboard (no passenger trip planning, fares or passenger feedback), and a
  rule-based scheduler (no ML).

Default parameters: 3 loop routes × 8 stops; 12 buses (3 per route plus 3 in
reserve); 40-passenger capacity; a 20-minute starting interval; a service day of
05:00–23:00; 1 real second = 1 simulated minute.

---

## 2. Architecture and data flow

```
Simulator ──publish(confirm)──▶ exchange transit.events (topic, durable)
                                   │ binding "#"
                                   ▼
                       queue transit.events.process (durable, competing consumers)
                                   │                       └─ rejects ─▶ transit.events.dead
                                   ▼
                       Worker × N ── one transaction per event ──▶ PostgreSQL
                                                                     │
       Scheduler ◀── reads stop_visits / passenger_journeys ─────────┤
           └── writes recommendations, route_scheduler_state ───────▶│
                                                                     │
       API/Dashboard ◀── reads state, recommendations ───────────────┤
           └── approve: update route_schedules (version+1) ─────────▶│
                                                                     │
       Simulator ◀── polls route_schedules every sim minute ─────────┘
           └── applies newer version at the route's next terminal departure
               and emits schedule.applied
```

**The control loop:**
1. The scheduler inserts a `recommendations` row with status `pending`.
2. The operator approves it. In one transaction, the API sets the status, inserts
   an `operator_decisions` row, and updates `route_schedules` with the new
   `interval_minutes` and `buses_assigned`, `version + 1`, and the
   `recommendation_id`. With `AUTO_APPROVE=true`, the scheduler does this itself.
3. The simulator sees the higher version within one simulated minute. At that
   route's next departure, it switches interval, pulls buses from the reserve (or
   releases them), and emits `schedule.applied` and `vehicle.assigned` events.
4. The worker marks the recommendation `applied` and sets
   `route_schedules.applied_version`.

**Reallocating a bus between routes:** in the same approval, lower the donor
route's `buses_assigned` and raise the receiving route's. The simulator releases
the donor's bus to the reserve when it finishes its current trip. The receiving
route takes it after `fleet.deadhead_minutes` of driving empty.

---

## 3. Contracts: do not break these

### 3.1 Events (`transit/common/events.py`)
- Every RabbitMQ message is JSON for one pydantic model. Parse it with
  `parse_event(body)`.
- The routing key and the AMQP `type` both equal `event_type`. The AMQP
  `message_id` equals `event_id`. Messages are persistent.
- Envelope fields: `event_id`, `run_id`, `sim_minute` (simulated minutes since
  service start), `clock` ("HH:MM"), `emitted_at` (UTC).
- Vehicle events carry `bus_id` and `seq`, which starts at 1 for each bus in each
  run and only increases.
- There are 15 types: `run.started`, `run.finished`, `vehicle.assigned`,
  `vehicle.departed_terminal`, `vehicle.arrived_stop`, `vehicle.departed_stop`,
  `vehicle.completed_trip`, `vehicle.delayed`, `vehicle.breakdown`,
  `vehicle.recovered`, `passenger.arrived`, `passenger.boarded`,
  `passenger.denied`, `passenger.alighted`, `schedule.applied`.
- To change the contract, add **optional** fields only. Renaming or removing a
  field breaks every consumer. Run `pytest tests/test_events.py` after any change.

### 3.2 Database (`transit/common/models.py`)
- The schema is owned by models + Alembic. To change it: edit `models.py`, run
  `alembic revision --autogenerate -m "what changed"`, review the generated file,
  then commit both.
- The **"Who writes what"** table at the top of `models.py` is the ownership
  contract. Don't write to a table you don't own without agreeing first.
- `*_minute` columns are simulated minutes; `*_at` columns are wall-clock time.
- All dynamic data is keyed by `run_id`. The *active run* is the most recent
  `sim_runs` row.

### 3.3 Reliability rules (proposal commitments)
- **Ack after commit:** the worker acks a message only after its DB transaction
  commits.
- **Idempotency:** insert `event_id` into `processed_events` in the *same*
  transaction as the update. If it's already there, skip the update and still ack.
- **Ordering guard:** ignore a vehicle event when `seq <= vehicle_state.last_seq`
  **for the same run_id**. A different `run_id` means a new run, so reset the guard.
- **Capacity:** the simulator decides boarding, so a bus never exceeds capacity.
  Workers update a bus's row under `SELECT ... FOR UPDATE` so concurrent workers
  can't interleave updates to the same bus.
- **Poison messages:** reject with `requeue=False`. They go to the dead-letter
  queue `transit.events.dead`.

### 3.4 Event to table mapping (for the worker)

| Event | Table updates |
|---|---|
| `run.started` | insert `sim_runs` (scenario, seed, config) |
| `run.finished` | `sim_runs.summary`, `finished_at` |
| `vehicle.assigned` | `vehicle_state.route_id` (null = reserve), `status` |
| `vehicle.departed_terminal` | `vehicle_state` (trip_id, status=in_service, occupancy) |
| `vehicle.arrived_stop` | `vehicle_state` (stop_id, stop_index, status=at_stop, occupancy) |
| `vehicle.departed_stop` | `vehicle_state` (status=in_service, occupancy), plus insert `stop_visits` |
| `vehicle.completed_trip` | `vehicle_state` (status=idle, occupancy=0) |
| `vehicle.breakdown` / `recovered` | `vehicle_state.status` = broken_down / in_service |
| `vehicle.delayed` | `vehicle_state.last_sim_minute` (optionally log it) |
| `passenger.arrived` | insert `passenger_journeys` |
| `passenger.boarded` | `passenger_journeys.boarded_minute`, `vehicle_id` |
| `passenger.denied` | `passenger_journeys.times_denied += 1` |
| `passenger.alighted` | `passenger_journeys.alighted_minute` |
| `schedule.applied` | `route_schedules.applied_version`; `recommendations.status = 'applied'` |

Every vehicle event also sets `last_seq`, `last_sim_minute` and `run_id`.

### 3.5 Configuration
- `config/simulation.yaml` plus `config/scenarios/*.yaml`, loaded by
  `load_simulation_config()`.
- Overrides use `--set a.b=c` or `SIM_SET`.
- `config/scheduler.yaml` is loaded by `load_scheduler_config()` into typed models
  (`SchedulerConfig` in `config.py`), so a typo or bad value fails at startup.
- Infrastructure URLs come only from the env vars `DATABASE_URL` and
  `RABBITMQ_URL`.
- Don't hard-code parameters in code. Add them to the YAML instead.

---

## 4. What is done

| Area | Files | Notes |
|---|---|---|
| Config loader | `transit/common/config.py` | Layered YAML, validation with clear errors |
| Event contract | `transit/common/events.py` | 15 types, discriminated-union parser |
| RabbitMQ | `transit/common/messaging.py` | Topology, DLQ, `ReliablePublisher` (confirms, persistent, reconnect). The consumer is still to do |
| Schema | `transit/common/models.py`, `migrations/` | 14 tables, initial migration |
| Seed | `transit/db/seed.py` | Idempotent. Resets schedules and vehicle state on each `docker compose up`, but **not while a simulator is running** (Compose reruns `migrate` whenever a dependent service starts, e.g. `docker compose start worker`). Schedule `version` never goes backwards. Creates the operator login. Derives `historical_demand` from the config |
| Auth helpers | `transit/common/security.py` | PBKDF2 `hash_password` / `verify_password` |
| Simulator | `transit/simulator/` | See below |
| Deployment | `docker-compose.yml`, `docker/`, `.env.example`, `Makefile` | Health-checked startup order, restart policies, RabbitMQ UI, config mounted read-only |
| Tests | `tests/test_config.py`, `test_events.py`, `test_simulator.py` | 21 passing |

### Simulator facts you can rely on
- **Arrivals:** a non-homogeneous Poisson process per stop (hourly profile × stop
  weight × route popularity × surges). Each stop has its **own random stream**, so
  the same seed gives the same passengers whatever schedule runs. That's what
  makes fixed-vs-adaptive comparisons fair, and there's a test for it.
- **Routes:** each route is a loop. Passengers ride forward to a later stop or
  back to the terminal. Boarding is FIFO up to capacity, and everyone left over
  gets a `passenger.denied` event.
- **Disruptions:** random per-segment delays, optional random breakdowns, and
  scheduled breakdowns for demos.
- **Pluggable inputs and outputs:**
  - `ScheduleSource`: `DatabaseScheduleSource` (live), `FixedScheduleSource`
    (baseline), `CallbackScheduleSource` (experiments, which can call scheduler
    functions in-process).
  - `EventSink`: rabbitmq, jsonl, memory or none.
- **Metrics:** `TransitSimulation.run()` returns a summary (served, left behind,
  still waiting, avg/p95/max wait, per route), which is also sent in
  `run.finished`.
- **Restarts:** a restart begins a **new run** with a new `run_id`. It follows the
  current `route_schedules`, but buses and passengers in motion are reset.
- **Verified end to end:** the simulator publishes to RabbitMQ and polls Postgres.
  Updating `route_schedules` mid-run was applied at the next departure and
  produced `schedule.applied`.

Reference numbers for the default seed of 42 (headless):

| Scenario | Fixed schedule | Hand-tuned (R1 → 10 min, 6 buses at 07:00) |
|---|---|---|
| `rush_hour` | avg wait 21.1 min, p95 76.6 min, 776 passengers left behind | avg 10.0, p95 25.4, 145 left behind |

---

## 5. What is left

Each file exists with a comment stating its purpose and expected outcome. Start
there.

### Event worker (`transit/worker/`)
- [x] Consume loop: manual ack, prefetch, reconnect, graceful SIGTERM (`consumer.py`, `__main__.py`)
- [x] Idempotency with `processed_events` in the same transaction as the update
- [x] Handlers for all 15 event types, using the table in §3.4 (`handlers.py`)
- [x] Per-bus `seq` guard (reset on a new run_id) and `FOR UPDATE` row locking
- [x] Tests in `tests/test_worker.py`
- [x] Remove `profiles: [full]` from the `worker` service in `docker-compose.yml`

### Scheduler (`transit/scheduler/`)
- [x] `demand_score.py`: weighted score, EMA, hysteresis peak labels (pure functions)
- [x] `policy.py`: interval formula, 5–30 min clamp, fleet cap, reserve target, reallocation, wait-saving estimate, cooldown
- [x] `service.py`: window queries, `route_scheduler_state`, recommendation rows, decline suppression and escalation, auto-approve
- [x] Tests in `tests/test_scheduler.py`; remove `profiles: [full]` from `scheduler`

Notes for whoever builds on the scheduler:
- **API: don't rewrite the approval transaction.** Call
  `transit.scheduler.service.approve_recommendation(session, rec, operator=..., decided_minute=...)`
  and `decline_recommendation(...)` inside your request's transaction. They lock the
  `route_schedules` rows, bump `version`, update the donor route on a reallocation, write
  `operator_decisions`, and raise `RecommendationError` (return HTTP 409) if the
  recommendation isn't pending or the reserve no longer has the buses.
- **Reallocation:** when `reallocate_from_route_id` is set, all `buses_delta` buses come
  from that donor route (none from the reserve). The donor's interval is lengthened to
  match, never beyond `interval.max_minutes`.
- **Labels** use pure hysteresis: `peak` at or above `upper_threshold`, `off_peak` at or
  below `lower_threshold`, and in between the label stays as it was. A run starts as `normal`.
- **Pending recommendations** keep their buses reserved. Each evaluation replaces a pending
  one (status `superseded`) if the new suggestion differs by `min_change_minutes`, or if no
  change is warranted any more.
- **Experiments:** `policy.plan_routes` + `demand_score.observe` run without a database. With
  auto-approve on `rush_hour` (seed 42), the loop gives avg wait 11.6 min / p95 30.5 min,
  vs 21.1 / 76.6 fixed.
- **Superseded approvals:** approving a recommendation marks any earlier approved-but-not-yet-applied
  one for the same route `superseded`, because the simulator only applies the newest version.
- **Docker test (rush_hour, speed 0.25, auto-approve, worker stopped ~30 s mid-run):** all 5,355
  passengers and 19,371 events recorded exactly once, backlog of ~2,900 drained in 8 s, dead-letter
  queue empty, avg wait 11.5 min / p95 27.8 min.
- `AUTO_APPROVE` from the environment overrides `operator.auto_approve` in the YAML. Compose
  always sets it (default `false`).

### API (`transit/api/`)
- [x] App, routers, health endpoint, serving `dashboard/dist`
- [x] Login using the `operators` table and `verify_password`; protect approve/decline
- [x] Approve/decline endpoints (the transaction in §2)
- [x] Read endpoints for routes, vehicles, stops and metrics; schemas in `schemas.py`
- [x] Tests in `tests/test_api.py`

Notes:
- `GET /api/overview` returns everything the dashboard shows in one call; the other read
  endpoints (`/run`, `/routes`, `/vehicles`, `/stops`, `/metrics`, `/system`,
  `/recommendations`) return the same pieces separately. Full list at `/docs`.
- `GET /api/system` reads queue depth, dead letters and consumer (worker) count from RabbitMQ
  with a passive `queue_declare` (no management plugin), cached 2 s (`transit/api/broker.py`).
- Tokens are HMAC-signed with `API_SECRET_KEY` (stdlib only, 12 h). Reads are open; approve and
  decline need `Authorization: Bearer <token>`. Approve/decline call the scheduler's
  `approve_recommendation` / `decline_recommendation`, and return 409 on conflicts.

### Dashboard (`dashboard/`)
- [x] Scaffold Vite + React + TS (output to `dist/`), with the dev proxy to `:8000`
- [x] Login, network map (with "last reported" age), route status, recommendations panel, warnings
- [x] Poll every 1–2 s
- [x] Once it builds, remove `profiles: [full]` from `api` in `docker-compose.yml`

Notes:
- Positions are marked out of date when the worker hasn't stored ANY event for 5 s, not per
  bus: a bus sends nothing while driving between stops, so a quiet bus is normal.
- Pending cards are listed oldest-first so new ones never shift the buttons under the cursor.
- Types in `dashboard/src/api/types.ts` mirror `transit/api/schemas.py`.

Fixes made while testing the dashboard against the live stack:
- The simulator now emits `schedule.applied` for each route's **starting** schedule, so
  `applied_version` is right from minute 0 (it used to stay empty until the first change).
- A pending suggestion stays unchanged for `operator.pending_minutes` (30 sim min) before it
  can be replaced. Without this, demand noise replaced cards every few seconds and nothing
  could be approved (one run had 30 replacements and 0 decisions).
- A pending suggestion to RELEASE a bus no longer counts as a free bus for other routes until
  it's approved; pending suggestions that ADD buses still reserve them.

### Experiments (`transit/experiments/`)
- [ ] Fixed vs adaptive runs over several seeds (using `CallbackScheduleSource` and the scheduler's pure functions)
- [ ] Metrics and CSV/charts for the report
- [ ] Fault-tolerance measurements: worker kill and recovery time, throughput with 1 vs N workers

### Integration and report
- [ ] Full `docker compose up --scale worker=3` run on a clean machine
- [ ] Demo rehearsal (README "Demo script")
- [ ] Final report and contribution matrix

---

## 6. How to work in this repo

- **Run tests:** `python -m pytest -q`. Tests must pass before you push.
- **Develop against real infrastructure without running your service in Docker:**
  run `make infra` (= `docker compose -f docker-compose.yml -f docker-compose.dev.yml up
  postgres rabbitmq migrate`; the dev overlay publishes ports 5432 and 5672, which the
  base file doesn't, so evaluators never hit a port clash), then run your service locally
  with
  `DATABASE_URL=postgresql+psycopg://transit:transit@localhost:5432/transit RABBITMQ_URL=amqp://transit:transit@localhost:5672/%2F`.
- **Generate realistic events with no broker:** run
  `python -m transit.simulator --scenario quick_test --sink jsonl --schedule fixed`
  and read `out/events.jsonl`. In tests, use `MemorySink`
  (see `tests/test_simulator.py`).
- **Feed the queue for worker development:** run
  `python -m transit.simulator --scenario quick_test --speed 0.05` with
  RabbitMQ running. It publishes about 4,000 events in about 10 seconds.
- **Branches:** use one branch per component (`feature/worker`, `feature/scheduler`,
  and so on), merged by pull request into `main`.
- **Never** edit `events.py` or `models.py` silently. Announce the change in the
  group chat and update this file.
- **Keep this file current:** when you finish an item, tick it in §5, and add
  anything the next person needs to know.
