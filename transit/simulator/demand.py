"""Passenger demand model: arrival rates by stop and time of day.

Arrivals at each stop follow a non-homogeneous Poisson process with rate

    rate(t) = base_rate_per_stop * stop.weight * route.popularity
              * hourly_profile[hour(t)] * product(active surge multipliers)

sampled exactly by thinning. Each stop has its OWN random stream derived from
the run seed and the stop id, so the arrival sequence at a stop is identical
across runs with the same seed, no matter how buses behave. That is what makes
the fixed-vs-adaptive comparison fair.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from transit.common.config import RouteConfig, SimulationConfig, StopConfig, parse_clock


@dataclass(frozen=True)
class SurgeWindow:
    start_minute: float   # sim minutes since service start
    end_minute: float
    multiplier: float


class DemandModel:
    def __init__(self, cfg: SimulationConfig):
        self.cfg = cfg
        self.start_of_day = cfg.time.start_minute
        self.profile = cfg.demand.hourly_profile
        self.surges: dict[str, list[SurgeWindow]] = {}
        for s in cfg.demand.surges:
            self.surges.setdefault(s.route, []).append(SurgeWindow(
                parse_clock(s.start) - self.start_of_day,
                parse_clock(s.end) - self.start_of_day,
                s.multiplier,
            ))

    def hour_of_day(self, sim_minute: float) -> int:
        return int((self.start_of_day + sim_minute) // 60) % 24

    def time_multiplier(self, route_id: str, sim_minute: float) -> float:
        m = self.profile[self.hour_of_day(sim_minute)]
        for w in self.surges.get(route_id, []):
            if w.start_minute <= sim_minute < w.end_minute:
                m *= w.multiplier
        return m

    def stop_rate(self, route: RouteConfig, stop: StopConfig, sim_minute: float) -> float:
        """Expected passengers per minute at this stop right now."""
        return (self.cfg.demand.base_rate_per_stop * stop.weight * route.popularity
                * self.time_multiplier(route.id, sim_minute))

    def max_stop_rate(self, route: RouteConfig, stop: StopConfig) -> float:
        """Upper bound on stop_rate over the day (needed for thinning)."""
        surge = 1.0
        for w in self.surges.get(route.id, []):
            surge *= max(1.0, w.multiplier)
        return self.cfg.demand.base_rate_per_stop * stop.weight * route.popularity * max(self.profile) * surge


def stream(seed: int, *parts: str) -> random.Random:
    """Independent, reproducible random stream for one component (str seeding is stable)."""
    return random.Random(":".join([str(seed), *parts]))
