// Small display helpers shared by the components.

const ROUTE_VARS = ["--r1", "--r2", "--r3"];

export function routeColor(routeId: string | null | undefined): string {
  if (!routeId) return "var(--faint)";
  const n = parseInt(routeId.replace(/\D/g, ""), 10);
  return `var(${ROUTE_VARS[(isNaN(n) ? 0 : n - 1) % ROUTE_VARS.length]})`;
}

export function fmtMin(value: number | null | undefined, digits = 1): string {
  return value == null ? "–" : `${value.toFixed(digits)} min`;
}

export function fmtNum(value: number | null | undefined): string {
  return value == null ? "–" : value.toLocaleString();
}

export function fmtAge(seconds: number | null | undefined): string {
  if (seconds == null) return "–";
  if (seconds < 1) return "just now";
  if (seconds < 60) return `${Math.round(seconds)} s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
  return `${(seconds / 3600).toFixed(1)} h ago`;
}

export const LABEL_TEXT: Record<string, string> = { peak: "Peak", normal: "Normal", off_peak: "Off-peak" };

export function labelClass(label: string): string {
  return label === "peak" ? "warn" : label === "off_peak" ? "" : "accent";
}

/** Occupancy colour: green when there's room, amber when filling, red when nearly full. */
export function loadColor(ratio: number): string {
  if (ratio >= 0.9) return "var(--bad)";
  if (ratio >= 0.6) return "var(--warn)";
  return "var(--good)";
}

/** Positions are out of date when the worker hasn't stored ANY event for this long (stopped or
 *  lagging). A single bus going quiet is normal: it sends nothing while driving between stops. */
export const STALE_SECONDS = 5;

export function isLagging(lagSeconds: number | null | undefined, running: boolean): boolean {
  return running && lagSeconds != null && lagSeconds > STALE_SECONDS;
}
