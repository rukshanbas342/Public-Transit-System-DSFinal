import pytest
from pydantic import ValidationError

from transit.common.config import (
    apply_override,
    deep_merge,
    format_clock,
    load_scheduler_config,
    load_simulation_config,
    parse_clock,
)


def test_base_config_loads_with_expected_parameters():
    cfg = load_simulation_config(use_env=False)
    assert cfg.scenario == "baseline"
    assert cfg.fleet.size == 12 and cfg.fleet.capacity == 40
    assert len(cfg.routes) == 3 and all(len(r.stops) == 8 for r in cfg.routes)
    assert cfg.reserve_size == 3
    assert cfg.time.duration_minutes == 18 * 60


def test_scenario_overlay_merges_without_losing_base_values():
    cfg = load_simulation_config("rush_hour", use_env=False)
    assert cfg.demand.surges[0].route == "R1"
    assert cfg.demand.base_rate_per_stop == 0.15  # untouched base value


def test_cli_overrides_win_and_parse_types():
    cfg = load_simulation_config(overrides=["fleet.capacity=60", "routes.0.initial_interval=15"], use_env=False)
    assert cfg.fleet.capacity == 60
    assert cfg.routes[0].initial_interval == 15


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("SIM_SEED", "7")
    monkeypatch.setenv("SIM_SET", "fleet.size=14;demand.base_rate_per_stop=0.3")
    cfg = load_simulation_config()
    assert cfg.run.seed == 7 and cfg.fleet.size == 14 and cfg.demand.base_rate_per_stop == 0.3


def test_invalid_config_fails_loudly():
    with pytest.raises(ValidationError):
        load_simulation_config(overrides=["fleet.size=5"], use_env=False)  # routes need 9 buses
    with pytest.raises(FileNotFoundError):
        load_simulation_config("does_not_exist", use_env=False)


def test_helpers():
    assert parse_clock("07:30") == 450 and format_clock(450) == "07:30"
    assert deep_merge({"a": {"b": 1, "c": 2}}, {"a": {"b": 3}}) == {"a": {"b": 3, "c": 2}}
    data = {"x": {"y": 1}}
    apply_override(data, "x.z=[1, 2]")
    assert data["x"]["z"] == [1, 2]


def test_scheduler_config_loads():
    sc = load_scheduler_config()
    assert sc.interval.min_minutes == 5 and sc.interval.max_minutes == 30
