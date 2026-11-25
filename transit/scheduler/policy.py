"""Interval calculation and fleet constraints. Pure functions, no I/O.

Decides WHAT to recommend for each route:

  * interval = 60 / (passengers_per_hour / (capacity * target_load)), clamped to [min, max]
  * buses needed = ceil(round_trip / interval); the interval can never be shorter than
    round_trip / buses, so a bus shortage lengthens the suggested interval
  * extra buses come from the reserve. Outside a peak, reserve_target buses stay free.
    If the reserve can't cover it, borrow from the quietest non-peak route, but only while
    that route is quieter, keeps enough buses for its own demand (at least one), and stays
    within max_minutes. A warning is attached.
  * estimated wait saving = (current_interval - suggested_interval) / 2
  * nothing is suggested while a route is in cooldown (one round trip after a change), while
    a declined suggestion is suppressed (unless demand grew by escalation_factor), or when
    the interval would change by less than min_change_minutes

`plan_routes` evaluates all routes together so two routes never claim the same reserve bus.
Experiments can call it directly with live engine state (no database).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Iterable, Mapping

from transit.common.config import DwellConfig, IntervalConfig, RouteConfig, SchedulerConfig
from transit.scheduler.demand_score import PEAK, RouteMemory

_EPS = 1e-9


# --------------------------------------------------------------------------- #
# Building blocks
# --------------------------------------------------------------------------- #
def round_trip_minutes(route: RouteConfig, dwell: DwellConfig) -> float:
    """Same estimate as TransitSimulation.round_trip_estimate: driving + an empty dwell per stop."""
    return route.travel_minutes + len(route.stops) * dwell.base_seconds / 60.0


def clamp_interval(minutes: float, cfg: IntervalConfig) -> float:
    return min(max(minutes, cfg.min_minutes), cfg.max_minutes)


def target_interval(passengers_per_hour: float, capacity: int, cfg: IntervalConfig) -> float:
    """60 / (pax_per_hour / (capacity * target_load)), clamped. No demand -> max interval."""
    if passengers_per_hour <= 0:
        return cfg.max_minutes
    trips_per_hour = passengers_per_hour / (capacity * cfg.target_load)
    return clamp_interval(60.0 / trips_per_hour, cfg)


def buses_needed(round_trip: float, interval: float) -> int:
    return max(1, math.ceil(round_trip / interval - _EPS))


def wait_saving(current_interval: float, suggested_interval: float) -> float:
    """Average wait is half the headway, so this is the expected change in wait per passenger."""
    return (current_interval - suggested_interval) / 2


def cooldown_minutes(cfg: SchedulerConfig, round_trip: float) -> float:
    return round_trip if cfg.fleet.cooldown == "round_trip" else float(cfg.fleet.cooldown)


def suppression_minutes(cfg: SchedulerConfig, round_trip: float) -> float:
    if cfg.operator.decline_suppression == "cooldown":
        return cooldown_minutes(cfg, round_trip)
    return float(cfg.operator.decline_suppression)


def blocked_reason(memory: RouteMemory, now: float, score: float, cfg: SchedulerConfig) -> str | None:
    """Why no new suggestion may be made for this route right now (None = free to suggest)."""
    if memory.cooldown_until_minute is not None and now < memory.cooldown_until_minute:
        return "cooldown"
    if memory.suppressed_until_minute is not None and now < memory.suppressed_until_minute:
        declined_at = memory.suppressed_score
        if declined_at is None:
            return "declined"
        escalated = (score >= declined_at * cfg.operator.escalation_factor) if declined_at > 0 \
            else score > declined_at
        if not escalated:
            return "declined"
    return None


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RouteView:
    """Everything the policy needs to know about one route at evaluation time."""

    route_id: str
    interval: float               # current route_schedules.interval_minutes
    buses: int                    # current route_schedules.buses_assigned
    round_trip: float
    passengers_per_hour: float    # observed in the window
    score: float                  # smoothed demand score
    label: str
    blocked: str | None = None    # from blocked_reason()


@dataclass(frozen=True)
class Pending:
    """A recommendation still waiting for the operator: its buses are already spoken for."""

    recommendation_id: int
    route_id: str
    suggested_interval: float
    buses_delta: int
    reallocate_from: str | None = None
    created_minute: float | None = None     # None = old enough to be replaced


@dataclass(frozen=True)
class Proposal:
    route_id: str
    current_interval: float
    suggested_interval: float
    current_buses: int
    suggested_buses: int
    reallocate_from: str | None
    donor_buses_after: int | None
    donor_interval_after: float | None
    est_wait_saving: float
    score: float
    label: str
    reason: str
    warning: str | None = None
    supersedes: int | None = None   # pending recommendation id this replaces

    @property
    def buses_delta(self) -> int:
        return self.suggested_buses - self.current_buses


def donor_interval_after(donor: RouteView, buses_after: int, cfg: IntervalConfig) -> float:
    return clamp_interval(max(donor.interval, donor.round_trip / buses_after), cfg)


def _max_donation(donor: RouteView, buses_on_route: int, wanted: int, capacity: int,
                  cfg: IntervalConfig) -> int:
    """How many buses a donor can spare: it keeps at least one, keeps enough for its own
    demand, and stays within max_minutes."""
    own_need = buses_needed(donor.round_trip, target_interval(donor.passengers_per_hour, capacity, cfg))
    for k in range(min(wanted, buses_on_route - max(own_need, 1)), 0, -1):
        if donor.round_trip / (buses_on_route - k) <= cfg.max_minutes + _EPS:
            return k
    return 0


def plan_route(
    route: RouteView,
    others: Iterable[RouteView],
    assigned: Mapping[str, int],
    fleet_size: int,
    capacity: int,
    cfg: SchedulerConfig,
) -> Proposal | None:
    """Suggest a new interval (and bus count) for one route, or None if no change is warranted.

    `assigned` is buses per route after counting other routes' pending claims; the reserve is
    whatever the fleet has left over.
    """
    icfg = cfg.interval
    current_buses = assigned.get(route.route_id, route.buses)
    reserve = fleet_size - sum(assigned.values())

    want = target_interval(route.passengers_per_hour, capacity, icfg)
    need = buses_needed(route.round_trip, want)
    extra = need - current_buses
    new_buses, donor, donor_after, warning = need, None, None, None
    source = ""

    if extra > 0:
        keep_free = 0 if route.label == PEAK else cfg.fleet.reserve_target
        from_reserve = max(0, min(extra, reserve - keep_free))
        best_donor, from_donor = None, 0
        if from_reserve < extra:
            donors = sorted((o for o in others if o.label != PEAK and o.route_id != route.route_id
                             and o.score < route.score),
                            key=lambda o: (o.score, o.route_id))
            for d in donors:
                k = _max_donation(d, assigned.get(d.route_id, d.buses), extra, capacity, icfg)
                if k > 0:
                    best_donor, from_donor = d, k
                    break
        if from_donor > from_reserve:
            donor, gained = best_donor, from_donor
            donor_buses = assigned.get(donor.route_id, donor.buses) - gained
            donor_after = (donor_buses, donor_interval_after(donor, donor_buses, icfg))
            source = f"; +{gained} from {donor.route_id}"
            warning = (f"no reserve bus free: reallocating {gained} bus(es) from {donor.route_id} "
                       f"(its interval becomes {donor_after[1]:.1f} min)")
        else:
            gained = from_reserve
            source = f"; +{gained} from reserve" if gained else ""
        new_buses = current_buses + gained
        if gained < extra:
            shortfall = (f"no buses available: {gained} of {extra} extra bus(es) found, "
                         f"interval limited by fleet")
            warning = f"{warning}; {shortfall}" if warning else shortfall
    elif extra < 0:
        source = f"; release {-extra} to reserve"

    suggested = clamp_interval(max(want, route.round_trip / max(new_buses, 1)), icfg)
    suggested = round(suggested, 1)
    if abs(suggested - route.interval) < icfg.min_change_minutes:
        return None

    reason = (f"{route.passengers_per_hour:.0f} pax/h (score {route.score:.2f}, {route.label}): "
              f"{want:.1f} min needs {need} bus(es) on a {route.round_trip:.0f} min round trip"
              f"{source}")
    return Proposal(
        route_id=route.route_id,
        current_interval=route.interval,
        suggested_interval=suggested,
        current_buses=current_buses,
        suggested_buses=new_buses,
        reallocate_from=donor.route_id if donor else None,
        donor_buses_after=donor_after[0] if donor_after else None,
        donor_interval_after=donor_after[1] if donor_after else None,
        est_wait_saving=round(wait_saving(route.interval, suggested), 2),
        score=route.score,
        label=route.label,
        reason=reason,
        warning=warning,
    )


def _apply_claim(assigned: dict[str, int], route_id: str, delta: int, donor: str | None, sign: int) -> None:
    """Reserve the buses an unapproved suggestion would take. A suggested RELEASE frees nothing
    until it's approved, so it isn't counted (another route can't plan on that bus yet)."""
    if delta < 0 and not donor:
        return
    assigned[route_id] = assigned.get(route_id, 0) + sign * delta
    if donor:
        assigned[donor] = assigned.get(donor, 0) - sign * delta


@dataclass(frozen=True)
class Plan:
    proposals: list[Proposal]
    stale: list[int]          # pending recommendation ids no longer warranted at all


def plan_routes(
    routes: list[RouteView],
    fleet_size: int,
    capacity: int,
    cfg: SchedulerConfig,
    pending: Mapping[str, Pending] | None = None,
    now: float | None = None,
) -> Plan:
    """Plan every route in one pass, busiest first, sharing one view of the fleet.

    Pending recommendations keep their buses reserved. A route with a pending suggestion is
    re-planned: the new proposal replaces it (supersedes) only when it differs by at least
    min_change_minutes, and if no change is warranted any more the pending one is reported stale.
    A pending suggestion younger than operator.pending_minutes is left alone, so the operator
    has time to read and decide before it changes under them.
    """
    pending = dict(pending or {})
    assigned = {r.route_id: r.buses for r in routes}
    for p in pending.values():
        _apply_claim(assigned, p.route_id, p.buses_delta, p.reallocate_from, +1)

    proposals: list[Proposal] = []
    stale: list[int] = []
    for route in sorted(routes, key=lambda r: (-r.score, r.route_id)):
        if route.blocked:
            continue
        own = pending.get(route.route_id)
        if own and now is not None and own.created_minute is not None \
                and now - own.created_minute < cfg.operator.pending_minutes:
            continue                      # still fresh: keep it (its buses stay claimed)
        if own:
            _apply_claim(assigned, own.route_id, own.buses_delta, own.reallocate_from, -1)
        others = [r for r in routes if r.route_id != route.route_id]
        proposal = plan_route(route, others, assigned, fleet_size, capacity, cfg)
        if proposal is None:
            if own:
                stale.append(own.recommendation_id)
            continue
        if own and abs(proposal.suggested_interval - own.suggested_interval) < cfg.interval.min_change_minutes:
            _apply_claim(assigned, own.route_id, own.buses_delta, own.reallocate_from, +1)
            continue
        if own:
            proposal = replace(proposal, supersedes=own.recommendation_id)
        _apply_claim(assigned, proposal.route_id, proposal.buses_delta, proposal.reallocate_from, +1)
        proposals.append(proposal)
    return Plan(proposals, stale)
