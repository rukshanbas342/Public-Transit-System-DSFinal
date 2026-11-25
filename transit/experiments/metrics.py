"""Summaries of paired fixed/adaptive simulation runs."""
from statistics import mean, stdev


def compare(rows):
    by_seed = {}
    for row in rows:
        by_seed.setdefault((row['scenario'], row['seed']), {})[row['schedule']] = row
    pairs = []
    for (scenario, seed), modes in sorted(by_seed.items()):
        if not {'fixed', 'adaptive'} <= modes.keys():
            continue
        fixed, adaptive = modes['fixed'], modes['adaptive']
        item = {'scenario': scenario, 'seed': seed}
        for field in ('avg_wait', 'p95_wait', 'served', 'still_waiting', 'passengers_left_behind'):
            a, b = fixed[field], adaptive[field]
            item[f'fixed_{field}'] = a
            item[f'adaptive_{field}'] = b
            item[f'change_{field}'] = round(b - a, 3) if a is not None and b is not None else None
        a, b = fixed['avg_wait'], adaptive['avg_wait']
        item['wait_reduction_percent'] = round(100 * (a - b) / a, 2) if a and b is not None else None
        pairs.append(item)
    return pairs


def aggregate(pairs):
    values = [r['wait_reduction_percent'] for r in pairs if r['wait_reduction_percent'] is not None]
    return {'paired_trials': len(pairs), 'mean_wait_reduction_percent': round(mean(values), 2) if values else None,
            'sd_wait_reduction_percent': round(stdev(values), 2) if len(values) > 1 else None}
