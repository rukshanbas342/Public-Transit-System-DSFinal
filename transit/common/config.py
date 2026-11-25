"""Configuration loading for every service.

Layers, later wins:
    1. config/simulation.yaml              (base)
    2. config/scenarios/<name>.yaml        (scenario overlay, deep-merged)
    3. overrides: "a.b.c=value" strings     (CLI --set, or env SIM_SET separated by ';')
    4. shortcut env vars: SIM_SCENARIO, SIM_SEED, SIM_SPEED

The result is validated into typed pydantic models so a typo fails loudly at
startup instead of silently mid-run.
"""

from __future__ import annotations

import copy
import os
from datetime import datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

CONFIG_DIR = Path(os.environ.get("TRANSIT_CONFIG_DIR", Path(__file__).resolve().parents[2] / "config"))


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #
def parse_clock(value: str) -> int:
    """'07:30' -> minutes after midnight (450)."""
    hours, minutes = value.strip().split(":")
    return int(hours) * 60 + int(minutes)


def format_clock(minutes_after_midnight: float) -> str:
    m = int(round(minutes_after_midnight)) % (24 * 60)
    return f"{m // 60:02d}:{m % 60:02d}"


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class RunConfig(BaseModel):
    seed: int = 42
    run_id: str | None = None


class TimeConfig(BaseModel):
    service_start: str = "05:00"
    service_end: str = "23:00"
    realtime_seconds_per_sim_minute: float = Field(1.0, ge=0)
    schedule_poll_minutes: float = Field(1, gt=0)

    @property
    def start_minute(self) -> int:
        return parse_clock(self.service_start)

    @property
    def duration_minutes(self) -> int:
        return parse_clock(self.service_end) - parse_clock(self.service_start)

    @model_validator(mode="after")
    def _check_window(self) -> "TimeConfig":
        if self.duration_minutes <= 0:
            raise ValueError("time.service_end must be after time.service_start")
        return self


class DwellConfig(BaseModel):
    base_seconds: float = 20
    per_boarding_seconds: float = 3
    per_alighting_seconds: float = 2


class FleetConfig(BaseModel):
    size: int = Field(12, ge=1)
    capacity: int = Field(40, ge=1)
    deadhead_minutes: float = Field(10, ge=0)
    dwell: DwellConfig = DwellConfig()


class StopConfig(BaseModel):
    id: str
    name: str
    travel_to_next: float = Field(gt=0)
    weight: float = Field(1.0, ge=0)


class RouteConfig(BaseModel):
    id: str
    name: str
    initial_interval: float = Field(gt=0)
    initial_buses: int = Field(ge=0)
    popularity: float = Field(1.0, ge=0)
    stops: list[StopConfig] = Field(min_length=2)

    @property
    def travel_minutes(self) -> float:
        """Driving time for one loop, excluding dwell."""
        return sum(s.travel_to_next for s in self.stops)


class SurgeConfig(BaseModel):
    route: str
    start: str
    end: str
    multiplier: float = Field(gt=0)


class DemandConfig(BaseModel):
    base_rate_per_stop: float = Field(0.15, ge=0)
    hourly_profile: list[float] = Field(min_length=24, max_length=24)
    surges: list[SurgeConfig] = []


class DelayConfig(BaseModel):
    probability_per_segment: float = Field(0.05, ge=0, le=1)
    min_minutes: float = 1
    max_minutes: float = 5


class RandomBreakdownConfig(BaseModel):
    probability_per_trip: float = Field(0.0, ge=0, le=1)
    min_minutes: float = 10
    max_minutes: float = 30


class ScheduledBreakdown(BaseModel):
    bus: str
    at: str
    minutes: float = Field(gt=0)


class DisruptionConfig(BaseModel):
    delays: DelayConfig = DelayConfig()
    random_breakdowns: RandomBreakdownConfig = RandomBreakdownConfig()
    scheduled_breakdowns: list[ScheduledBreakdown] = []


class PublishingConfig(BaseModel):
    sink: Literal["rabbitmq", "jsonl", "memory", "none"] = "rabbitmq"
    jsonl_path: str = "out/events.jsonl"


class SimulationConfig(BaseModel):
    run: RunConfig = RunConfig()
    time: TimeConfig = TimeConfig()
    fleet: FleetConfig = FleetConfig()
    routes: list[RouteConfig] = Field(min_length=1)
    demand: DemandConfig
    disruptions: DisruptionConfig = DisruptionConfig()
    publishing: PublishingConfig = PublishingConfig()
    scenario: str = "baseline"

    @model_validator(mode="after")
    def _check_consistency(self) -> "SimulationConfig":
        route_ids = [r.id for r in self.routes]
        if len(set(route_ids)) != len(route_ids):
            raise ValueError("route ids must be unique")
        stop_ids = [s.id for r in self.routes for s in r.stops]
        if len(set(stop_ids)) != len(stop_ids):
            raise ValueError("stop ids must be unique across all routes")
        assigned = sum(r.initial_buses for r in self.routes)
        if assigned > self.fleet.size:
            raise ValueError(f"routes assign {assigned} buses but fleet.size is {self.fleet.size}")
        for surge in self.demand.surges:
            if surge.route not in route_ids:
                raise ValueError(f"surge references unknown route {surge.route}")
        bus_ids = set(self.bus_ids)
        for bd in self.disruptions.scheduled_breakdowns:
            if bd.bus not in bus_ids:
                raise ValueError(f"scheduled breakdown references unknown bus {bd.bus}")
        return self

    # ---- derived values used across services --------------------------------
    @property
    def bus_ids(self) -> list[str]:
        return [f"B{i:02d}" for i in range(1, self.fleet.size + 1)]

    @property
    def reserve_size(self) -> int:
        return self.fleet.size - sum(r.initial_buses for r in self.routes)

    def route(self, route_id: str) -> RouteConfig:
        for r in self.routes:
            if r.id == route_id:
                return r
        raise KeyError(route_id)

    def resolved_run_id(self) -> str:
        if self.run.run_id:
            return self.run.run_id
        return f"run-{datetime.now():%Y%m%d-%H%M%S}-{self.run.seed}"


class EvaluationConfig(BaseModel):
    every_minutes: float = Field(5, gt=0)       # sim minutes between evaluations
    window_minutes: float = Field(15, gt=0)     # sim minutes of data per evaluation
    ema_alpha: float = Field(0.4, gt=0, le=1)
    poll_seconds: float = Field(2, gt=0)        # real seconds between checks of the sim clock


class DemandWeights(BaseModel):
    arrivals_per_minute: float = 1.0
    avg_occupancy_ratio: float = 2.0
    denied_per_minute: float = 3.0


class DemandOffsets(BaseModel):
    time_of_day: float = 0.5
    anticipation_minutes: float = Field(20, ge=0)
    popularity: float = 1.0
    peak_factor: float = Field(1.5, gt=0)      # an hour is a "known peak" at >= factor x daily mean


class DemandScoreConfig(BaseModel):
    weights: DemandWeights = DemandWeights()
    offsets: DemandOffsets = DemandOffsets()


class PeakDetectionConfig(BaseModel):
    upper_threshold: float = 3.0
    lower_threshold: float = 1.5

    @model_validator(mode="after")
    def _check_order(self) -> "PeakDetectionConfig":
        if self.lower_threshold >= self.upper_threshold:
            raise ValueError("peak_detection.lower_threshold must be below upper_threshold")
        return self


class IntervalConfig(BaseModel):
    target_load: float = Field(0.8, gt=0, le=1)
    min_minutes: float = Field(5, gt=0)
    max_minutes: float = Field(30, gt=0)
    min_change_minutes: float = Field(2, ge=0)

    @model_validator(mode="after")
    def _check_range(self) -> "IntervalConfig":
        if self.min_minutes > self.max_minutes:
            raise ValueError("interval.min_minutes must not exceed interval.max_minutes")
        return self


class SchedulerFleetConfig(BaseModel):
    reserve_target: int = Field(1, ge=0)
    cooldown: Literal["round_trip"] | float = "round_trip"


class OperatorConfig(BaseModel):
    auto_approve: bool = False
    decline_suppression: Literal["cooldown"] | float = "cooldown"
    escalation_factor: float = Field(1.25, ge=1)
    pending_minutes: float = Field(30, ge=0)   # a pending suggestion stays unchanged at least this long


class SchedulerConfig(BaseModel):
    evaluation: EvaluationConfig = EvaluationConfig()
    demand_score: DemandScoreConfig = DemandScoreConfig()
    peak_detection: PeakDetectionConfig = PeakDetectionConfig()
    interval: IntervalConfig = IntervalConfig()
    fleet: SchedulerFleetConfig = SchedulerFleetConfig()
    operator: OperatorConfig = OperatorConfig()


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def deep_merge(base: dict, overlay: dict) -> dict:
    """Recursively merge overlay into a copy of base (lists are replaced)."""
    result = copy.deepcopy(base)
    for key, value in (overlay or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def apply_override(data: dict, assignment: str) -> None:
    """Apply 'fleet.capacity=60' in place. Values are parsed as YAML (numbers, bools, lists)."""
    if "=" not in assignment:
        raise ValueError(f"override must look like key.path=value, got {assignment!r}")
    path, raw = assignment.split("=", 1)
    keys = [k for k in path.strip().split(".") if k]
    node = data
    for key in keys[:-1]:
        if isinstance(node, list):
            node = node[int(key)]
        else:
            node = node.setdefault(key, {})
    last = keys[-1]
    value = yaml.safe_load(raw)
    if isinstance(node, list):
        node[int(last)] = value
    else:
        node[last] = value


def _read_yaml(path: Path) -> dict:
    with open(path) as fh:
        return yaml.safe_load(fh) or {}


def load_simulation_config(
    scenario: str | None = None,
    overrides: list[str] | None = None,
    config_dir: Path | None = None,
    use_env: bool = True,
) -> SimulationConfig:
    config_dir = Path(config_dir or CONFIG_DIR)
    data = _read_yaml(config_dir / "simulation.yaml")

    scenario = scenario or (os.environ.get("SIM_SCENARIO") if use_env else None) or "baseline"
    scenario_path = config_dir / "scenarios" / f"{scenario}.yaml"
    if not scenario_path.exists():
        available = sorted(p.stem for p in (config_dir / "scenarios").glob("*.yaml"))
        raise FileNotFoundError(f"unknown scenario {scenario!r}; available: {available}")
    data = deep_merge(data, _read_yaml(scenario_path))
    data["scenario"] = scenario

    all_overrides: list[str] = []
    if use_env:
        if os.environ.get("SIM_SEED"):
            all_overrides.append(f"run.seed={os.environ['SIM_SEED']}")
        if os.environ.get("SIM_SPEED"):
            all_overrides.append(f"time.realtime_seconds_per_sim_minute={os.environ['SIM_SPEED']}")
        if os.environ.get("SIM_RUN_ID"):
            all_overrides.append(f"run.run_id={os.environ['SIM_RUN_ID']}")
        if os.environ.get("SIM_SET"):
            all_overrides.extend(s for s in os.environ["SIM_SET"].split(";") if s.strip())
    all_overrides.extend(overrides or [])
    for assignment in all_overrides:
        apply_override(data, assignment)

    return SimulationConfig.model_validate(data)


def load_scheduler_config(config_dir: Path | None = None) -> SchedulerConfig:
    config_dir = Path(config_dir or CONFIG_DIR)
    return SchedulerConfig.model_validate(_read_yaml(config_dir / "scheduler.yaml"))


# --------------------------------------------------------------------------- #
# Infrastructure settings (env only - these differ per machine, not per test)
# --------------------------------------------------------------------------- #
class InfraSettings(BaseModel):
    database_url: str = "postgresql+psycopg://transit:transit@localhost:5432/transit"
    rabbitmq_url: str = "amqp://transit:transit@localhost:5672/%2F"

    @classmethod
    def from_env(cls) -> "InfraSettings":
        return cls(
            database_url=os.environ.get("DATABASE_URL", cls.model_fields["database_url"].default),
            rabbitmq_url=os.environ.get("RABBITMQ_URL", cls.model_fields["rabbitmq_url"].default),
        )

    @field_validator("database_url")
    @classmethod
    def _psycopg3(cls, v: str) -> str:
        # Accept plain postgresql:// URLs and point SQLAlchemy at psycopg 3.
        if v.startswith("postgresql://"):
            return "postgresql+psycopg://" + v[len("postgresql://"):]
        return v
