// Recorded missions (mission logging plan §7): API types and the pure helpers the
// Missions page uses (timeline lanes, replay state at a time, formatting).
import type {Tone} from "../components/ui";

export const MISSIONS_API = "api/logbook/missions";

export interface MissionSummary {
  mission_id: string;
  name?: string | null;
  state?: string;
  session_id?: string | null;
  experiment?: string;
  t_start?: number;
  t_end?: number | null;
  duration_s?: number | null;
  robots: string[];
  n_goals: number;
  goals_done: number;
  goals_failed: number;
  check?: string | null;
  check_counts?: Record<string, number> | null;
  collect?: string | null;
  collect_machines?: Record<string, string>;
  size?: number | null;
  end_reason?: string | null;
}

export interface RelocRecord {
  state: string;
  t?: number;
  operator?: string | null;
  method?: string | null;
  reason?: string | null;
  note?: string | null;
}

export interface SessionGroup {
  session_id: string | null;
  experiment?: string | null;
  state?: string;
  t_start?: number;
  t_end?: number | null;
  robots: string[];
  mode?: string | null;
  base_station?: string | null;
  launch?: Record<string, unknown>;
  reloc: Record<string, RelocRecord>;
  missions: MissionSummary[];
}

export interface MissionList {
  sessions: SessionGroup[];
  current?: {mission?: {mission_id: string} | null; gate?: {ok: boolean; reasons: string[]}} | null;
}

export interface CheckItem {
  id: string;
  title: string;
  status: "pass" | "warn" | "fail" | "skip";
  detail: string;
  first?: Record<string, unknown>;
  items?: Record<string, unknown>[];
  n_items?: number;
}

export interface Report {
  mission_id: string;
  status: "pass" | "warn" | "fail";
  t_checked: number;
  export_blocked: boolean;
  counts: Record<string, number>;
  checks: CheckItem[];
}

export interface MissionDetail {
  summary: MissionSummary;
  mission: Record<string, unknown> & {t_start?: number; t_end?: number; robots?: string[]; goals?: string[]};
  session?: Record<string, unknown> | null;
  session_reloc?: {robots?: Record<string, RelocRecord & {history?: RelocRecord[]}>} | null;
  reloc?: {robots?: Record<string, RelocRecord & {history?: RelocRecord[]}>} | null;
  manifest?: Record<string, unknown> | null;
  report?: Report | null;
  collect?: {state?: string; phase?: string; machines?: Record<string, {state: string; error?: string; why?: string}>} | null;
  map_state?: Record<string, unknown> | null;
  has_dsg?: boolean;
}

export interface MissionEvent {
  t_base: number;
  t_wall: number;
  _machine: string;
  source: string;
  type: string;
  robot?: string | null;
  goal_id?: string | null;
  plan_id?: string | null;
  seq?: number;
  payload: Record<string, unknown>;
}

export interface EventPage {
  events: MissionEvent[];
  total: number;
  t_range: [number | null, number | null];
  facets: {machine: Record<string, number>; source: Record<string, number>; type: Record<string, number>};
}

export interface GoalRobot {
  plan_id: string;
  engine?: string | null;
  solve_s?: number | null;
  planned_m?: number | null;
  driven_m?: number | null;
  time_to_reach_s?: number | null;
  result?: string | null;
  leases: number;
  stalls: number;
  repairs: number;
}

export interface GoalRecord {
  goal_id: string;
  t_sent?: number;
  robot?: string;
  kind?: string;
  payload?: unknown;
  card?: Record<string, unknown>;
  approved?: Record<string, unknown>;
  stages?: {t: number; robot?: string | null; from?: string | null; to: string; detail?: string; plan_id?: string | null}[];
  outcomes?: Record<string, {t: number; result: string; duration_s?: number}>;
  images?: {sym?: string; i?: number; src?: string; $file?: string; error?: string}[];
  robots?: Record<string, GoalRobot>;
}

export interface ChatTurn {
  t: number;
  type: string;
  payload: Record<string, unknown>;
  goal_id?: string | null;
}

export interface Stats {
  goals: (GoalRobot & {goal_id: string; robot: string; kind?: string})[];
  robots: Record<string, {driven_m: number; goals: number; done: number; failed: number}>;
  llm: {calls: number; in_tok: number; out_tok: number; cost_usd: number; latency_ms_p50?: number | null; latency_ms_max?: number | null};
  link: Record<string, [number, number | null, number | null, number | null][]>;
  clock: Record<string, [number, number][]>;
}

export type PoseRow = [number, number, number, number | null]; // t, x, y, yaw

export interface Replay {
  mission_id: string;
  t_start?: number;
  t_end?: number;
  t_range: [number, number];
  poses: Record<string, PoseRow[]>;
  plans: {t: number; robot: string; plan_id?: string; engine?: string; path: [number, number][]; len_m?: number}[];
  changes: {t: number | null; seq?: number; op?: string; source?: string; symbol?: string; label?: string; xy?: [number, number] | null}[];
  reloc: {t: number; type: string; robot: string; state: string}[];
  stages: {t: number; robot: string; goal_id?: string; to: string}[];
  map: {objects: [number, number, string | null][]; places: [number, number][]};
}

// ------------------------------------------------------------------ formatting
export function fmtDuration(s?: number | null): string {
  if (s == null || !isFinite(s)) return "–";
  if (s < 60) return `${s.toFixed(s < 10 ? 1 : 0)} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min ${Math.round(s - m * 60)} s`;
  return `${Math.floor(m / 60)} h ${m % 60} min`;
}

export function fmtTime(t?: number | null): string {
  if (t == null) return "–";
  const d = new Date(t * 1000);
  return d.toLocaleString(undefined, {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit"});
}

export function fmtClock(t: number, t0: number): string {
  const s = Math.max(0, t - t0);
  const m = Math.floor(s / 60);
  return `${m}:${String(Math.floor(s - m * 60)).padStart(2, "0")}`;
}

export function fmtBytes(n?: number | null): string {
  if (n == null) return "–";
  if (n < 1024) return `${n} B`;
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(0)} kB`;
  if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(1)} MB`;
  return `${(n / 1024 ** 3).toFixed(2)} GB`;
}

export function checkTone(s?: string | null): Tone {
  return s === "pass" ? "ok" : s === "warn" ? "warn" : s === "fail" ? "err" : "off";
}

export function collectTone(s?: string | null): Tone {
  return s === "done" || s === "collected" ? "ok" : s === "pending" || s === "deferred" || s === "live_only" ? "warn" : s === "failed" ? "err" : s === "running" || s === "collecting" ? "info" : "off";
}

export function resultTone(s?: string | null): Tone {
  return s === "done" ? "ok" : s === "failed" || s === "plan_failed" ? "err" : s === "no_plan" || s === "preempted" ? "warn" : "off";
}

// ------------------------------------------------------------------ timeline
/** The swimlane an event goes in: base chat / planner / base, or the robot. */
export function laneOf(e: MissionEvent): string {
  if (e._machine !== "base") return e._machine;
  if (e.type.startsWith("chat.") || e.type === "llm.call" || e.type.startsWith("sam3.")) return "chat";
  if (["omniplanner", "goal_manager"].includes(e.source) || e.type.startsWith("plan.") || e.type.startsWith("repair.")) return "planner";
  return "base";
}

export const EVENT_COLORS: [string, string][] = [
  ["mission.", "var(--accent)"],
  ["goal.", "var(--info)"],
  ["plan.", "#8b5cf6"],
  ["action.", "var(--ok)"],
  ["lease.", "var(--err)"],
  ["reloc.", "#d97706"],
  ["loc.", "#d97706"],
  ["chat.", "#0ea5e9"],
  ["llm.", "#0ea5e9"],
  ["follow.", "#14b8a6"],
];

export function eventColor(type: string): string {
  for (const [p, c] of EVENT_COLORS) if (type.startsWith(p)) return c;
  return "var(--muted)";
}

/** Noisy periodic types the timeline hides unless asked for. */
export const PERIODIC = ["robot.pose", "link.stats", "follow.progress", "clock.offset", "link.robot"];

export function lanes(events: MissionEvent[]): string[] {
  const order = ["chat", "planner", "base"];
  const robots = new Set<string>();
  for (const e of events) {
    const l = laneOf(e);
    if (!order.includes(l)) robots.add(l);
  }
  return [...order, ...[...robots].sort()];
}

// ------------------------------------------------------------------ replay
/** Pose of a robot at ``t`` (linear between samples; null before the first). */
export function poseAt(rows: PoseRow[] | undefined, t: number): {x: number; y: number; yaw: number | null} | null {
  if (!rows || !rows.length || t < rows[0][0]) return null;
  let lo = 0;
  let hi = rows.length - 1;
  if (t >= rows[hi][0]) return {x: rows[hi][1], y: rows[hi][2], yaw: rows[hi][3]};
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (rows[mid][0] <= t) lo = mid;
    else hi = mid;
  }
  const [t0, x0, y0, w0] = rows[lo];
  const [t1, x1, y1] = rows[hi];
  const f = t1 > t0 ? (t - t0) / (t1 - t0) : 0;
  return {x: x0 + (x1 - x0) * f, y: y0 + (y1 - y0) * f, yaw: w0};
}

/** The trail (last ``window`` s) of a robot up to ``t``. */
export function trailUntil(rows: PoseRow[] | undefined, t: number, window = 120): [number, number][] {
  if (!rows) return [];
  return rows.filter(r => r[0] <= t && r[0] >= t - window).map(r => [r[1], r[2]] as [number, number]);
}

/** Each robot's newest plan at ``t``. */
export function activePlans(plans: Replay["plans"], t: number): Record<string, Replay["plans"][number]> {
  const out: Record<string, Replay["plans"][number]> = {};
  for (const p of plans) if (p.t <= t) out[p.robot] = p;
  return out;
}

export function changesUntil(changes: Replay["changes"], t: number): Replay["changes"] {
  return changes.filter(c => c.t != null && c.t <= t);
}

export function relocAt(reloc: Replay["reloc"], t: number): Record<string, string> {
  const out: Record<string, string> = {};
  for (const r of reloc) if (r.t <= t) out[r.robot] = r.state;
  return out;
}

export function goalStageAt(stages: Replay["stages"], t: number): Record<string, string> {
  const out: Record<string, string> = {};
  for (const s of stages) if (s.t <= t) out[s.robot] = s.to;
  return out;
}

/** World bounds of everything the replay draws (with a margin). */
export function replayBounds(r: Replay): [number, number, number, number] {
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity; // prettier-ignore
  const add = (x: number, y: number) => {
    x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x); y1 = Math.max(y1, y); // prettier-ignore
  };
  for (const rows of Object.values(r.poses)) for (const p of rows) add(p[1], p[2]);
  for (const p of r.plans) for (const [x, y] of p.path) add(x, y);
  // the map only around what happened (a big prior would shrink the robots to dots)
  const poseless = !isFinite(x0);
  for (const [x, y] of r.map.objects) if (poseless) add(x, y);
  for (const c of r.changes) if (c.xy) add(c.xy[0], c.xy[1]);
  if (!isFinite(x0)) return [-10, -10, 10, 10];
  const m = Math.max(3, 0.08 * Math.max(x1 - x0, y1 - y0));
  return [x0 - m, y0 - m, x1 + m, y1 + m];
}

export const ROBOT_COLORS = ["#2563eb", "#dc2626", "#16a34a", "#9333ea", "#ea580c", "#0891b2"];
export function robotColor(robots: string[], r: string): string {
  const i = robots.indexOf(r);
  return ROBOT_COLORS[(i < 0 ? 0 : i) % ROBOT_COLORS.length];
}

export function fileUrl(missionId: string, path: string): string {
  return `${MISSIONS_API}/${encodeURIComponent(missionId)}/file?path=${encodeURIComponent(path)}`;
}

/** A base event's ``$file`` (relative to the base dir: ``missions/<id>/...``) as a path in the mission. */
export function missionRel(missionId: string, ref: string): string {
  const pre = `missions/${missionId}/`;
  return ref.startsWith(pre) ? ref.slice(pre.length) : ref;
}
