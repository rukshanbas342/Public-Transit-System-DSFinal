"""Write CSV and JSON experiment artifacts using Python standard library."""
import csv
import json
from pathlib import Path
from transit.experiments.metrics import compare, aggregate


def _csv(path, rows):
    if not rows:
        return
    with path.open('w', newline='', encoding='utf-8') as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def write_results(rows, output):
    dest = Path(output)
    dest.mkdir(parents=True, exist_ok=True)
    pairs = compare(rows)
    _csv(dest / 'runs.csv', rows)
    _csv(dest / 'paired_comparison.csv', pairs)
    summary = aggregate(pairs)
    (dest / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(f'Results written to {dest}; summary: {summary}')
