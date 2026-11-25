"""Paired headless scheduling experiments; no broker/database required.

Run: python -m transit.experiments.run_experiment --scenario rush_hour --seeds 1 2 3
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from transit.common.config import load_simulation_config, load_scheduler_config
from transit.scheduler import demand_score as ds, policy
from transit.simulator.engine import TransitSimulation
from transit.simulator.schedule_source import CallbackScheduleSource, FixedScheduleSource, initial_schedule, ScheduleEntry
from transit.simulator.sinks import NullSink
from transit.experiments.report import write_results


class AdaptiveController:
    """Auto-approves in-process policy proposals, based on observed simulation counters."""
    def __init__(self, cfg, scheduler_cfg):
        self.cfg, self.scfg = cfg, scheduler_cfg
        self.schedule = initial_schedule(cfg)
        self.sim = None
        self.last_time = 0.0
        self.last_counts = {r.id: (0, 0) for r in cfg.routes}
        self.memory = {r.id: ds.RouteMemory() for r in cfg.routes}
        self.next_eval = scheduler_cfg.evaluation.every_minutes
        self.changes = 0

    def poll(self, minute: float) -> dict[str, ScheduleEntry]:
        if self.sim is None or minute < self.next_eval:
            return dict(self.schedule)
        elapsed = max(minute - self.last_time, 1e-6)
        views = []
        for rid, state in self.sim.routes.items():
            metrics = self.sim.metrics[rid]
            old_arrivals, old_denied = self.last_counts[rid]
            window = ds.WindowMetrics(elapsed, metrics.arrived - old_arrivals,
                                      metrics.denied_events - old_denied, None)
            raw = ds.demand_score(window, self.scfg.demand_score).total
            mem = ds.observe(self.memory[rid], raw, minute, self.scfg)
            self.memory[rid] = mem
            self.last_counts[rid] = (metrics.arrived, metrics.denied_events)
            current = self.schedule[rid]
            views.append(policy.RouteView(rid, current.interval_minutes, current.buses_assigned,
                                          self.sim.round_trip_estimate(rid), window.passengers_per_hour,
                                          mem.ema_score or 0.0, mem.peak_label,
                                          policy.blocked_reason(mem, minute, mem.ema_score or 0.0, self.scfg)))
        planned = policy.plan_routes(views, self.cfg.fleet.size, self.cfg.fleet.capacity, self.scfg, now=minute)
        for proposal in planned.proposals:
            old = self.schedule[proposal.route_id]
            self.schedule[proposal.route_id] = ScheduleEntry(proposal.route_id, old.version + 1,
                proposal.suggested_interval, proposal.suggested_buses)
            self.memory[proposal.route_id] = replace(self.memory[proposal.route_id],
                cooldown_until_minute=minute + policy.cooldown_minutes(self.scfg, self.sim.round_trip_estimate(proposal.route_id)))
            self.changes += 1
            if proposal.reallocate_from:
                donor = self.schedule[proposal.reallocate_from]
                self.schedule[donor.route_id] = ScheduleEntry(donor.route_id, donor.version + 1,
                    proposal.donor_interval_after, proposal.donor_buses_after)
        self.last_time = minute
        self.next_eval = minute + self.scfg.evaluation.every_minutes
        return dict(self.schedule)


def paired_runs(scenario: str, seeds: list[int]):
    rows = []
    for seed in seeds:
        cfg = load_simulation_config(scenario, [f'run.seed={seed}', 'time.realtime_seconds_per_sim_minute=0',
                                                 'publishing.sink=none'], use_env=False)
        fixed = TransitSimulation(cfg, NullSink(), FixedScheduleSource(cfg), realtime=False)
        fixed_summary = fixed.run()
        controller = AdaptiveController(cfg, load_scheduler_config())
        adaptive = TransitSimulation(cfg, NullSink(), CallbackScheduleSource(controller.poll), realtime=False)
        controller.sim = adaptive
        adaptive_summary = adaptive.run()
        for mode, summary, changes in [('fixed', fixed_summary, 0), ('adaptive', adaptive_summary, controller.changes)]:
            totals = summary['totals']
            rows.append({'scenario': scenario, 'seed': seed, 'schedule': mode,
                         'arrived': totals['arrived'], 'served': totals['served'],
                         'still_waiting': totals['still_waiting'],
                         'passengers_left_behind': totals['passengers_left_behind'],
                         'avg_wait': totals['avg_wait'], 'p95_wait': totals['p95_wait'],
                         'events_published': summary['events_published'], 'recommendations_applied': changes})
        print(f'{scenario} seed={seed}: fixed wait={fixed_summary["totals"]["avg_wait"]}, '
              f'adaptive wait={adaptive_summary["totals"]["avg_wait"]}, proposals={controller.changes}')
    return rows


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--scenario', default='rush_hour')
    p.add_argument('--seeds', type=int, nargs='+', default=[1, 2, 3, 4, 5])
    p.add_argument('--output', default='out/experiments')
    args = p.parse_args()
    write_results(paired_runs(args.scenario, args.seeds), args.output)


if __name__ == '__main__':
    main()
