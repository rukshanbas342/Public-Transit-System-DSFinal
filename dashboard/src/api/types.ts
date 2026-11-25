// Mirrors transit/api/schemas.py. Keep the two in sync.
// *_minute = simulated minutes since service start; *_seconds = real seconds.

export interface RunInfo {
  run_id: string;
  scenario: string;
  seed: number;
  service_start: string;
  service_end: string;
  started_at: string;
  finished_at: string | null;
  sim_minute: number | null;
  clock: string | null;
  duration_minutes: number;
  summary: Record<string, unknown> | null;
}

export interface RouteStatus {
  route_id: string;
  name: string;
  round_trip_minutes: number;
  interval_minutes: number;
  buses_assigned: number;
  buses_on_route: number;
  schedule_version: number;
  applied_version: number | null;
  schedule_source: string;
  peak_label: "peak" | "normal" | "off_peak" | string;
  demand_score: number | null;
  cooldown_until_minute: number | null;
  arrived: number;
  served: number;
  waiting: number;
  left_behind: number;
  avg_wait: number | null;
  p95_wait: number | null;
  recent_avg_wait: number | null;
  avg_occupancy_ratio: number | null;
}

export interface VehicleStatus {
  vehicle_id: string;
  route_id: string | null;
  status: string;
  stop_id: string | null;
  stop_index: number | null;
  occupancy: number;
  capacity: number;
  last_sim_minute: number | null;
  updated_at: string | null;
  age_seconds: number | null;
}

export interface StopStatus {
  stop_id: string;
  route_id: string;
  stop_index: number;
  name: string;
  waiting: number;
}

export interface Recommendation {
  id: number;
  route_id: string;
  created_minute: number;
  created_clock: string | null;
  current_interval: number;
  suggested_interval: number;
  buses_delta: number;
  reallocate_from_route_id: string | null;
  demand_score: number;
  peak_label: string;
  est_wait_saving_minutes: number;
  reason: string;
  warning: string | null;
  status: string;
  decided_by: string | null;
  decided_minute: number | null;
}

export interface QueueStatus {
  name: string;
  messages: number;
  consumers: number;
}

export interface SystemHealth {
  database_ok: boolean;
  broker_ok: boolean;
  broker_error: string | null;
  events_queue: QueueStatus | null;
  dead_letter_queue: QueueStatus | null;
  workers: number | null;
  simulator_connected: boolean | null;
  events_processed: number;
  events_per_second: number;
  last_event_age_seconds: number | null;
  scheduler_last_evaluated_minute: number | null;
  scheduler_age_seconds: number | null;
  auto_approve_hint: boolean;
}

export interface Totals {
  arrived: number;
  served: number;
  waiting: number;
  left_behind: number;
  avg_wait: number | null;
  p95_wait: number | null;
}

export interface Overview {
  server_time: string;
  run: RunInfo | null;
  totals: Totals | null;
  routes: RouteStatus[];
  vehicles: VehicleStatus[];
  stops: StopStatus[];
  pending: Recommendation[];
  history: Recommendation[];
  system: SystemHealth;
}

export interface LoginResponse {
  token: string;
  username: string;
  expires_at: string;
}
