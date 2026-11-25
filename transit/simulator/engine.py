"""SimPy model of the bus network.

Processes
---------
  passenger_source(stop)   Poisson arrivals at one stop (see demand.py)
  dispatcher(route)        departs a bus from the terminal every `interval` minutes;
                           applies a newer schedule version right before a departure
  trip(route, bus)         drives one loop: board/alight at each stop, delays, breakdowns
  schedule_poller          asks the ScheduleSource for the current schedule
  fleet_manager            moves buses between routes and the reserve pool to match
                           each route's `buses_assigned`
  heartbeat                lets the sink service its connection while time passes

Every observable change is emitted as an event (transit/common/events.py).
The engine also keeps its own metrics so a run can be scored without a database.
"""

from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import dataclass, field
from typing import Iterable

import simpy
import simpy.rt

from transit.common.config import SimulationConfig, format_clock, parse_clock
from transit.common import events as ev
from transit.simulator.demand import DemandModel, stream
from transit.simulator.schedule_source import ScheduleEntry, ScheduleSource, initial_schedule
from transit.simulator.sinks import EventSink

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Runtime state
# --------------------------------------------------------------------------- #
@dataclass
class Passenger:
    id: str
    route_id: str
    origin_index: int
    destination_index: int     # index of the stop to get off at; len(stops) == back at terminal
    arrived_minute: float
    boarded_minute: float | None = None
    times_denied: int = 0


@dataclass
class Bus:
    id: str
    capacity: int
    route_id: str | None = None          # None = reserve pool
    passengers: list[Passenger] = field(default_factory=list)
    seq: int = 0
    status: str = "idle"                 # idle | in_service | deadhead | broken_down

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq

    @property
    def occupancy(self) -> int:
        return len(self.passengers)


@dataclass
class RouteState:
    cfg_index: int
    id: str
    stop_ids: list[str]
    travel: list[float]
    terminal: simpy.Store
    queues: list[deque]
    schedule: ScheduleEntry
    pending: ScheduleEntry | None = None
    buses: set[str] = field(default_factory=set)   # every bus assigned (terminal, on road, deadheading)
    trip_counter: int = 0
    passenger_counter: dict[int, int] = field(default_factory=dict)

    @property
    def n_stops(self) -> int:
        return len(self.stop_ids)

    @property
    def waiting(self) -> int:
        return sum(len(q) for q in self.queues)


@dataclass
class RouteMetrics:
    arrived: int = 0
    boarded: int = 0
    alighted: int = 0
    denied_events: int = 0
    passengers_denied: int = 0
    waits: list[float] = field(default_factory=list)
    trips: int = 0


# --------------------------------------------------------------------------- #
# Simulation
# --------------------------------------------------------------------------- #
class TransitSimulation:
    def __init__(
        self,
        cfg: SimulationConfig,
        sink: EventSink,
        schedule_source: ScheduleSource | None = None,
        realtime: bool | None = None,
    ):
        self.cfg = cfg
        self.sink = sink
        self.run_id = cfg.resolved_run_id()
        self.seed = cfg.run.seed
        self.demand = DemandModel(cfg)
        self.start_of_day = cfg.time.start_minute
        self.duration = cfg.time.duration_minutes

        factor = cfg.time.realtime_seconds_per_sim_minute
        use_rt = factor > 0 if realtime is None else realtime
        self.env: simpy.Environment = (
            simpy.rt.RealtimeEnvironment(factor=factor, strict=False) if use_rt and factor > 0
            else simpy.Environment()
        )

        self.schedule_source = schedule_source
        start_schedule = (schedule_source.poll(0.0) if schedule_source else None) or initial_schedule(cfg)

        self.buses: dict[str, Bus] = {b: Bus(b, cfg.fleet.capacity) for b in cfg.bus_ids}
        self.reserve: list[str] = []
        self.routes: dict[str, RouteState] = {}
        for i, r in enumerate(cfg.routes):
            self.routes[r.id] = RouteState(
                cfg_index=i,
                id=r.id,
                stop_ids=[s.id for s in r.stops],
                travel=[s.travel_to_next for s in r.stops],
                terminal=simpy.Store(self.env),
                queues=[deque() for _ in r.stops],
                schedule=start_schedule.get(r.id, initial_schedule(cfg)[r.id]),
                passenger_counter={j: 0 for j in range(len(r.stops))},
            )
        self.metrics: dict[str, RouteMetrics] = {r: RouteMetrics() for r in self.routes}
        self.delay_rng = {b: stream(self.seed, "delay", b) for b in self.buses}
        self.breakdown_rng = {b: stream(self.seed, "breakdown", b) for b in self.buses}
        self.scheduled_breakdowns = [
            {"bus": bd.bus, "at": parse_clock(bd.at) - self.start_of_day, "minutes": bd.minutes, "done": False}
            for bd in cfg.disruptions.scheduled_breakdowns
        ]
        self.finished = False

    # ---- helpers ------------------------------------------------------------ #
    def _clock(self) -> str:
        return format_clock(self.start_of_day + self.env.now)

    def _common(self) -> dict:
        return {"run_id": self.run_id, "sim_minute": round(self.env.now, 3), "clock": self._clock()}

    def emit(self, event: ev.BaseEvent) -> None:
        self.sink.publish(event)

    def _dwell_minutes(self, boarded: int, alighted: int) -> float:
        d = self.cfg.fleet.dwell
        return (d.base_seconds + boarded * d.per_boarding_seconds + alighted * d.per_alighting_seconds) / 60.0

    def round_trip_estimate(self, route_id: str) -> float:
        r = self.routes[route_id]
        return sum(r.travel) + r.n_stops * self._dwell_minutes(0, 0)

    # ---- setup -------------------------------------------------------------- #
    def _initial_assignment(self) -> None:
        free = list(self.buses)
        for route in self.routes.values():
            for _ in range(min(route.schedule.buses_assigned, len(free))):
                bus = self.buses[free.pop(0)]
                self._assign(bus, route.id, "initial assignment")
                route.terminal.put(bus)
        for bus_id in free:
            self._assign(self.buses[bus_id], None, "initial reserve")

    def _assign(self, bus: Bus, route_id: str | None, reason: str) -> None:
        previous = bus.route_id
        if previous is not None:
            self.routes[previous].buses.discard(bus.id)
        if previous is None and bus.id in self.reserve:
            self.reserve.remove(bus.id)
        bus.route_id = route_id
        if route_id is None:
            self.reserve.append(bus.id)
            self.reserve.sort()
            bus.status = "idle"
        else:
            self.routes[route_id].buses.add(bus.id)
        self.emit(ev.BusAssigned(**self._common(), bus_id=bus.id, seq=bus.next_seq(),
                                 from_route_id=previous, to_route_id=route_id, reason=reason))

    # ---- processes ---------------------------------------------------------- #
    def passenger_source(self, route_id: str, stop_index: int):
        route_cfg = self.cfg.routes[self.routes[route_id].cfg_index]
        stop_cfg = route_cfg.stops[stop_index]
        route = self.routes[route_id]
        rng = stream(self.seed, "arrivals", stop_cfg.id)
        lam_max = self.demand.max_stop_rate(route_cfg, stop_cfg)
        if lam_max <= 0:
            return
        t = 0.0
        while True:
            t += rng.expovariate(lam_max)
            accept = rng.random() < self.demand.stop_rate(route_cfg, stop_cfg, t) / lam_max
            # destination drawn for every candidate so the stream stays aligned
            destination = rng.randint(stop_index + 1, route.n_stops)
            if t >= self.duration:
                return
            if not accept:
                continue
            yield self.env.timeout(t - self.env.now)
            route.passenger_counter[stop_index] += 1
            p = Passenger(
                id=f"{self.run_id}:{stop_cfg.id}:{route.passenger_counter[stop_index]}",
                route_id=route_id, origin_index=stop_index,
                destination_index=destination, arrived_minute=self.env.now,
            )
            route.queues[stop_index].append(p)
            self.metrics[route_id].arrived += 1
            dest_id = route.stop_ids[destination % route.n_stops]
            self.emit(ev.PassengerArrived(**self._common(), passenger_id=p.id, route_id=route_id,
                                          stop_id=stop_cfg.id, destination_stop_id=dest_id))

    def dispatcher(self, route_id: str):
        route = self.routes[route_id]
        next_departure = 0.0
        while True:
            if self.env.now < next_departure:
                yield self.env.timeout(next_departure - self.env.now)
            self._apply_pending(route)
            if route.schedule.buses_assigned <= 0:
                next_departure = self.env.now + route.schedule.interval_minutes
                continue
            bus = yield route.terminal.get()
            if bus.route_id != route_id:      # released to reserve while waiting
                continue
            route.trip_counter += 1
            trip_id = f"{route_id}-T{route.trip_counter:04d}"
            self.env.process(self.trip(route, bus, trip_id, scheduled=next_departure))
            next_departure = max(next_departure + route.schedule.interval_minutes, self.env.now)

    def _apply_pending(self, route: RouteState) -> None:
        p = route.pending
        if p is None or p.version <= route.schedule.version:
            route.pending = None
            return
        route.schedule, route.pending = p, None
        log.info("%s applying schedule v%d: every %.1f min, %d buses",
                 route.id, p.version, p.interval_minutes, p.buses_assigned)
        self.emit(ev.ScheduleApplied(**self._common(), route_id=route.id, version=p.version,
                                     interval_minutes=p.interval_minutes, buses_assigned=p.buses_assigned,
                                     recommendation_id=p.recommendation_id))
        self._rebalance()

    def trip(self, route: RouteState, bus: Bus, trip_id: str, scheduled: float):
        m = self.metrics[route.id]
        bus.status = "in_service"
        depart_time = self.env.now
        breakdown_segment = None
        rb = self.cfg.disruptions.random_breakdowns
        rng = self.breakdown_rng[bus.id]
        if rb.probability_per_trip > 0 and rng.random() < rb.probability_per_trip:
            breakdown_segment = rng.randrange(route.n_stops)

        for i, stop_id in enumerate(route.stop_ids):
            yield from self._serve_stop(route, bus, trip_id, i)
            if i == 0:
                self.emit(ev.BusDepartedTerminal(
                    **self._common(), bus_id=bus.id, seq=bus.next_seq(), route_id=route.id,
                    trip_id=trip_id, scheduled_minute=round(scheduled, 3),
                    interval_minutes=route.schedule.interval_minutes, occupancy=bus.occupancy))

            yield from self._disruptions(route, bus, trip_id, stop_id, breakdown_segment == i)
            yield self.env.timeout(route.travel[i])

        # back at the terminal: everyone gets off
        self.emit(ev.BusArrivedStop(**self._common(), bus_id=bus.id, seq=bus.next_seq(), route_id=route.id,
                                    trip_id=trip_id, stop_id=route.stop_ids[0], stop_index=0,
                                    occupancy=bus.occupancy))
        for p in list(bus.passengers):
            self._alight(route, bus, trip_id, p, route.stop_ids[0])
        bus.passengers.clear()
        m.trips += 1
        self.emit(ev.BusCompletedTrip(**self._common(), bus_id=bus.id, seq=bus.next_seq(), route_id=route.id,
                                      trip_id=trip_id, round_trip_minutes=round(self.env.now - depart_time, 3)))
        bus.status = "idle"
        if len(route.buses) > route.schedule.buses_assigned:
            self._assign(bus, None, f"{route.id} needs fewer buses")
        else:
            route.terminal.put(bus)

    def _serve_stop(self, route: RouteState, bus: Bus, trip_id: str, i: int):
        stop_id = route.stop_ids[i]
        m = self.metrics[route.id]
        self.emit(ev.BusArrivedStop(**self._common(), bus_id=bus.id, seq=bus.next_seq(), route_id=route.id,
                                    trip_id=trip_id, stop_id=stop_id, stop_index=i, occupancy=bus.occupancy))
        leaving = [p for p in bus.passengers if p.destination_index == i]
        for p in leaving:
            bus.passengers.remove(p)
            self._alight(route, bus, trip_id, p, stop_id)

        queue = route.queues[i]
        boarded = 0
        while queue and bus.occupancy < bus.capacity:
            p = queue.popleft()
            p.boarded_minute = self.env.now
            bus.passengers.append(p)
            boarded += 1
            wait = self.env.now - p.arrived_minute
            m.boarded += 1
            m.waits.append(wait)
            self.emit(ev.PassengerBoarded(**self._common(), passenger_id=p.id, route_id=route.id,
                                          stop_id=stop_id, bus_id=bus.id, trip_id=trip_id,
                                          wait_minutes=round(wait, 3)))
        for p in queue:   # bus is full: everyone still waiting is left behind
            if p.times_denied == 0:
                m.passengers_denied += 1
            p.times_denied += 1
            m.denied_events += 1
            self.emit(ev.PassengerDenied(**self._common(), passenger_id=p.id, route_id=route.id,
                                         stop_id=stop_id, bus_id=bus.id, trip_id=trip_id))

        yield self.env.timeout(self._dwell_minutes(boarded, len(leaving)))
        self.emit(ev.BusDepartedStop(**self._common(), bus_id=bus.id, seq=bus.next_seq(), route_id=route.id,
                                     trip_id=trip_id, stop_id=stop_id, stop_index=i, occupancy=bus.occupancy,
                                     boarded=boarded, alighted=len(leaving), left_behind=len(queue),
                                     capacity=bus.capacity))

    def _alight(self, route: RouteState, bus: Bus, trip_id: str, p: Passenger, stop_id: str) -> None:
        self.metrics[route.id].alighted += 1
        self.emit(ev.PassengerAlighted(**self._common(), passenger_id=p.id, route_id=route.id, stop_id=stop_id,
                                       bus_id=bus.id, trip_id=trip_id,
                                       travel_minutes=round(self.env.now - (p.boarded_minute or self.env.now), 3)))

    def _disruptions(self, route: RouteState, bus: Bus, trip_id: str, stop_id: str, random_breakdown: bool):
        due = [b for b in self.scheduled_breakdowns
               if b["bus"] == bus.id and not b["done"] and b["at"] <= self.env.now]
        if due or random_breakdown:
            if due:
                due[0]["done"] = True
                minutes = due[0]["minutes"]
            else:
                rb = self.cfg.disruptions.random_breakdowns
                minutes = self.breakdown_rng[bus.id].uniform(rb.min_minutes, rb.max_minutes)
            bus.status = "broken_down"
            self.emit(ev.BusBrokeDown(**self._common(), bus_id=bus.id, seq=bus.next_seq(), route_id=route.id,
                                      trip_id=trip_id, after_stop_id=stop_id, expected_minutes=round(minutes, 2)))
            yield self.env.timeout(minutes)
            bus.status = "in_service"
            self.emit(ev.BusRecovered(**self._common(), bus_id=bus.id, seq=bus.next_seq(),
                                      route_id=route.id, trip_id=trip_id))

        d = self.cfg.disruptions.delays
        rng = self.delay_rng[bus.id]
        if d.probability_per_segment > 0 and rng.random() < d.probability_per_segment:
            delay = rng.uniform(d.min_minutes, d.max_minutes)
            self.emit(ev.BusDelayed(**self._common(), bus_id=bus.id, seq=bus.next_seq(), route_id=route.id,
                                    trip_id=trip_id, after_stop_id=stop_id, delay_minutes=round(delay, 2)))
            yield self.env.timeout(delay)

    def schedule_poller(self):
        while True:
            yield self.env.timeout(self.cfg.time.schedule_poll_minutes)
            if self.schedule_source is None:
                continue
            for route_id, entry in self.schedule_source.poll(self.env.now).items():
                route = self.routes.get(route_id)
                if route and entry.version > route.schedule.version:
                    if route.pending is None or entry.version > route.pending.version:
                        route.pending = entry

    def _rebalance(self) -> None:
        """Match each route's bus count to its schedule using the reserve pool."""
        # release surplus buses that are idle at a terminal right now
        for route in self.routes.values():
            while len(route.buses) > route.schedule.buses_assigned and route.terminal.items:
                bus = route.terminal.items.pop()
                self._assign(bus, None, f"{route.id} needs fewer buses")
        # fill deficits from the reserve (busiest deficit first, deterministic order)
        deficits = sorted(
            (r for r in self.routes.values() if len(r.buses) < r.schedule.buses_assigned),
            key=lambda r: (len(r.buses) - r.schedule.buses_assigned, r.id),
        )
        for route in deficits:
            while len(route.buses) < route.schedule.buses_assigned and self.reserve:
                bus = self.buses[self.reserve[0]]
                self._assign(bus, route.id, f"{route.id} needs more buses")
                self.env.process(self._deadhead(route, bus))

    def _deadhead(self, route: RouteState, bus: Bus):
        bus.status = "deadhead"
        yield self.env.timeout(self.cfg.fleet.deadhead_minutes)
        bus.status = "idle"
        if bus.route_id == route.id:
            route.terminal.put(bus)

    def fleet_manager(self):
        while True:
            yield self.env.timeout(1)
            self._rebalance()

    def heartbeat(self):
        while True:
            yield self.env.timeout(1)
            self.sink.tick()

    # ---- run ---------------------------------------------------------------- #
    def run(self) -> dict:
        self.emit(ev.RunStarted(**self._common(), scenario=self.cfg.scenario, seed=self.seed,
                                service_start=self.cfg.time.service_start, service_end=self.cfg.time.service_end,
                                config=self.cfg.model_dump(mode="json")))
        # Confirm the schedule each route starts with, so route_schedules.applied_version
        # reflects what is in effect from the first minute (not only after a later change).
        for route in self.routes.values():
            s = route.schedule
            self.emit(ev.ScheduleApplied(**self._common(), route_id=route.id, version=s.version,
                                         interval_minutes=s.interval_minutes, buses_assigned=s.buses_assigned,
                                         recommendation_id=s.recommendation_id))
        self._initial_assignment()
        for route in self.routes.values():
            for i in range(route.n_stops):
                self.env.process(self.passenger_source(route.id, i))
            self.env.process(self.dispatcher(route.id))
        self.env.process(self.schedule_poller())
        self.env.process(self.fleet_manager())
        self.env.process(self.heartbeat())

        self.env.run(until=self.duration)
        summary = self.summary()
        self.emit(ev.RunFinished(**self._common(), summary=summary))
        self.finished = True
        return summary

    def summary(self) -> dict:
        def stats(waits: Iterable[float]) -> dict:
            w = sorted(waits)
            if not w:
                return {"avg_wait": None, "p95_wait": None, "max_wait": None}
            p95 = w[min(len(w) - 1, math.ceil(0.95 * len(w)) - 1)]
            return {"avg_wait": round(sum(w) / len(w), 2), "p95_wait": round(p95, 2), "max_wait": round(w[-1], 2)}

        per_route = {}
        for rid, m in self.metrics.items():
            per_route[rid] = {
                "arrived": m.arrived, "boarded": m.boarded, "alighted": m.alighted,
                "passengers_left_behind": m.passengers_denied, "left_behind_events": m.denied_events,
                "still_waiting": self.routes[rid].waiting, "trips": m.trips,
                "final_interval": self.routes[rid].schedule.interval_minutes,
                "final_buses": self.routes[rid].schedule.buses_assigned,
                **stats(m.waits),
            }
        all_waits = [w for m in self.metrics.values() for w in m.waits]
        totals = {
            "arrived": sum(r["arrived"] for r in per_route.values()),
            "served": sum(r["boarded"] for r in per_route.values()),
            "passengers_left_behind": sum(r["passengers_left_behind"] for r in per_route.values()),
            "left_behind_events": sum(r["left_behind_events"] for r in per_route.values()),
            "still_waiting": sum(r["still_waiting"] for r in per_route.values()),
            **stats(all_waits),
        }
        return {"run_id": self.run_id, "scenario": self.cfg.scenario, "seed": self.seed,
                "sim_minutes": round(self.env.now, 1), "events_published": getattr(self.sink, "count", None),
                "totals": totals, "routes": per_route}
