"""Demand score and peak/off-peak labelling. Pure functions, no I/O.

Turns a window of route metrics into a smoothed demand score and a label:

  * score = sum(weight * variable) + offsets, with weights from scheduler.yaml
      variables: arrivals/min, average occupancy ratio, passengers left behind/min
      offsets:   time_of_day  - added when a known peak starts within anticipation_minutes
                 popularity   - weight * (current demand / historical demand - 1)
  * EMA smoothing with ema_alpha ("momentum")
  * hysteresis labels: >= upper_threshold -> peak, <= lower_threshold -> off_peak, and in
    between the previous label is kept, so a score hovering near one threshold can't flicker.
    The first evaluation of a run starts from "normal".
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Mapping

from transit.common.config import DemandScoreConfig, PeakDetectionConfig, SchedulerConfig

PEAK, NORMAL, OFF_PEAK = "peak", "normal", "off_peak"


@dataclass(frozen=True)
class WindowMetrics:
    """What one route did during the evaluation window."""

    minutes: float                         # window length actually covered (shorter at run start)
    arrivals: int                          # passengers who arrived at the route's stops
    denied: int                            # passengers left behind by a full bus (summed per stop visit)
    avg_occupancy_ratio: float | None      # mean occupancy / capacity when buses left stops; None = no visits

    @property
    def arrivals_per_minute(self) -> float:
        return self.arrivals / self.minutes if self.minutes > 0 else 0.0

    @property
    def passengers_per_hour(self) -> float:
        return self.arrivals_per_minute * 60

    @property
    def denied_per_minute(self) -> float:
        return self.denied / self.minutes if self.minutes > 0 else 0.0


@dataclass(frozen=True)
class ScoreBreakdown:
    arrivals: float
    occupancy: float
    denied: float
    time_of_day: float
    popularity: float

    @property
    def total(self) -> float:
        return self.arrivals + self.occupancy + self.denied + self.time_of_day + self.popularity


@dataclass(frozen=True)
class RouteMemory:
    """Per-route scheduler state kept between evaluations (mirrors route_scheduler_state)."""

    ema_score: float | None = None
    peak_label: str = NORMAL
    cooldown_until_minute: float | None = None
    suppressed_until_minute: float | None = None
    suppressed_score: float | None = None
    last_evaluated_minute: float | None = None


# --------------------------------------------------------------------------- #
# Known peaks from historical demand
# --------------------------------------------------------------------------- #
def peak_start_hours(historical: Mapping[int, float], peak_factor: float) -> list[int]:
    """Hours of day where historical demand rises to >= peak_factor x the daily mean."""
    if not historical:
        return []
    mean = sum(historical.values()) / len(historical)
    threshold = mean * peak_factor
    starts = []
    for hour in sorted(historical):
        before = historical.get((hour - 1) % 24, 0.0)
        if historical[hour] >= threshold > before:
            starts.append(hour)
    return starts


def peak_starts_soon(clock_minute: float, historical: Mapping[int, float], cfg: DemandScoreConfig) -> bool:
    """True when a known peak begins within the next anticipation_minutes (clock = minutes after midnight)."""
    horizon = cfg.offsets.anticipation_minutes
    for hour in peak_start_hours(historical, cfg.offsets.peak_factor):
        ahead = (hour * 60 - clock_minute) % (24 * 60)
        if 0 < ahead <= horizon:
            return True
    return False


# --------------------------------------------------------------------------- #
# Score, smoothing, labels
# --------------------------------------------------------------------------- #
def demand_score(
    metrics: WindowMetrics,
    cfg: DemandScoreConfig,
    *,
    historical_pph: float | None = None,
    peak_soon: bool = False,
) -> ScoreBreakdown:
    w = cfg.weights
    popularity = 0.0
    if historical_pph and historical_pph > 0:
        popularity = cfg.offsets.popularity * (metrics.passengers_per_hour / historical_pph - 1)
    return ScoreBreakdown(
        arrivals=w.arrivals_per_minute * metrics.arrivals_per_minute,
        occupancy=w.avg_occupancy_ratio * (metrics.avg_occupancy_ratio or 0.0),
        denied=w.denied_per_minute * metrics.denied_per_minute,
        time_of_day=cfg.offsets.time_of_day if peak_soon else 0.0,
        popularity=popularity,
    )


def ema(previous: float | None, value: float, alpha: float) -> float:
    if previous is None:
        return value
    return alpha * value + (1 - alpha) * previous


def hysteresis_label(previous: str, score: float, cfg: PeakDetectionConfig) -> str:
    if score >= cfg.upper_threshold:
        return PEAK
    if score <= cfg.lower_threshold:
        return OFF_PEAK
    return previous


def observe(memory: RouteMemory, raw_score: float, now: float, cfg: SchedulerConfig) -> RouteMemory:
    """Fold one new raw score into the route's memory (EMA + label)."""
    smoothed = ema(memory.ema_score, raw_score, cfg.evaluation.ema_alpha)
    return replace(
        memory,
        ema_score=smoothed,
        peak_label=hysteresis_label(memory.peak_label, smoothed, cfg.peak_detection),
        last_evaluated_minute=now,
    )
