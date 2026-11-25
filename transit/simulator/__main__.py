"""Run the simulator.

Examples
--------
  # live system (inside docker compose): publish to RabbitMQ, follow schedules from the DB
  python -m transit.simulator

  # no infrastructure at all: fast run, fixed schedule, print the summary
  python -m transit.simulator --scenario quick_test --sink none --schedule fixed

  # write events to a file to inspect what workers will receive
  python -m transit.simulator --scenario rush_hour --sink jsonl --speed 0 --schedule fixed

  # change any config value for one run
  python -m transit.simulator --set fleet.capacity=60 --set demand.base_rate_per_stop=0.25 --seed 7
"""

from __future__ import annotations

import argparse
import json
import signal
import sys

from transit.common.config import InfraSettings, load_simulation_config
from transit.common.logging_setup import setup_logging
from transit.simulator.engine import TransitSimulation
from transit.simulator.schedule_source import DatabaseScheduleSource, FixedScheduleSource
from transit.simulator.sinks import make_sink


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m transit.simulator", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--scenario", help="overlay from config/scenarios (default: env SIM_SCENARIO or baseline)")
    p.add_argument("--seed", type=int, help="random seed (default: config run.seed)")
    p.add_argument("--speed", type=float,
                   help="real seconds per simulated minute; 0 = as fast as possible")
    p.add_argument("--sink", choices=["rabbitmq", "jsonl", "memory", "none"], help="where events go")
    p.add_argument("--schedule", choices=["db", "fixed"],
                   help="db = follow route_schedules (default with rabbitmq); fixed = initial config only")
    p.add_argument("--run-id", help="override the generated run id")
    p.add_argument("--set", dest="overrides", action="append", default=[], metavar="KEY=VALUE",
                   help="override any config value, e.g. fleet.capacity=60 (repeatable)")
    p.add_argument("--summary-json", help="also write the end-of-run summary to this file")
    return p


def main(argv: list[str] | None = None) -> int:
    log = setup_logging("simulator")
    args = build_parser().parse_args(argv)

    overrides = list(args.overrides)
    if args.seed is not None:
        overrides.append(f"run.seed={args.seed}")
    if args.speed is not None:
        overrides.append(f"time.realtime_seconds_per_sim_minute={args.speed}")
    if args.run_id:
        overrides.append(f"run.run_id={args.run_id}")
    if args.sink:
        overrides.append(f"publishing.sink={args.sink}")
    cfg = load_simulation_config(args.scenario, overrides)

    infra = InfraSettings.from_env()
    sink = make_sink(cfg.publishing.sink, rabbitmq_url=infra.rabbitmq_url, jsonl_path=cfg.publishing.jsonl_path)
    schedule_mode = args.schedule or ("db" if cfg.publishing.sink == "rabbitmq" else "fixed")
    source = DatabaseScheduleSource(infra.database_url) if schedule_mode == "db" else FixedScheduleSource(cfg)

    sim = TransitSimulation(cfg, sink, source)
    log.info("run %s | scenario=%s seed=%d | %s-%s | %.2fs per sim minute | sink=%s schedule=%s",
             sim.run_id, cfg.scenario, cfg.run.seed, cfg.time.service_start, cfg.time.service_end,
             cfg.time.realtime_seconds_per_sim_minute, cfg.publishing.sink, schedule_mode)

    def _stop(signum, _frame):
        log.info("received signal %s, stopping", signum)
        sink.close()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _stop)

    try:
        summary = sim.run()
    finally:
        sink.close()

    print(json.dumps(summary, indent=2))
    if args.summary_json:
        with open(args.summary_json, "w") as fh:
            json.dump(summary, fh, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
