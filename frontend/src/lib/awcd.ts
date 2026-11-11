// AWCD change detection: the base station's merged change set
// (GET /api/change_detection/changes, hub topic awcd/changes). Drawn like
// RViz's awcd_visualizer: a wireframe box per change, green = added,
// red = removed (AWCD has no "moved"), label "<robots> | <kind> <id> | <class>
// | cc=… n=…". Positions are in the base station's target frame (the global
// map), so they are drawn as is.

export type AwcdKind = "added" | "removed";

export interface AwcdChange {
  key: string;
  kind: AwcdKind;
  id: number;
  /** the id to show: added = the robot's track id (``id`` may pass 2^53) */
  short_id?: number;
  label: string;
  center: [number, number, number];
  dims: [number, number, number];
  quat: [number, number, number, number]; // x, y, z, w
  stamp: number;
  change_confidence: number;
  confidence: number;
  frames: number;
  /** robots that reported it (added: the first one to see it) */
  robots: string[];
}

export interface AwcdState {
  available: boolean;
  error?: string | null;
  topic: string | null;
  t: number | null;
  frame_id: string | null;
  changes: AwcdChange[];
  counts: {added: number; removed: number};
  robots: Record<string, {t: number; added: number; removed: number}>;
}

/** awcd_visualizer's RED / GREEN */
export const AWCD_COLORS: Record<AwcdKind, string> = {added: "#00ff00", removed: "#ff0000"};

export function changeColor(kind: string): string {
  return AWCD_COLORS[kind as AwcdKind] ?? "#ffb020";
}

/** Ids of added objects are 64-bit (robot hash << 32 | track id): show the track id. */
export function shortId(c: Pick<AwcdChange, "id" | "short_id">): string {
  return String(c.short_id ?? c.id);
}

/** The 3D label, as awcd_visualizer's (robots instead of member ids). */
export function changeLabel(c: AwcdChange): string {
  const who = c.robots.length ? c.robots.join(", ") : "?";
  return `${who} | ${c.kind} ${shortId(c)} | ${c.label || "unknown"} | cc=${c.change_confidence.toFixed(2)} n=${c.frames}`;
}

export function fmtPos(p: number[]): string {
  return p.slice(0, 2).map(v => v.toFixed(1)).join(", ");
}

export function fmtClock(t: number | null | undefined): string {
  if (!t) return "-";
  return new Date(t * 1000).toLocaleTimeString([], {hour12: false});
}

/** Rotate v by unit quaternion q = [x, y, z, w]. */
function rot(q: number[], v: number[]): number[] {
  const [x, y, z, w] = q;
  // t = 2 q.xyz × v ; v' = v + w t + q.xyz × t
  const tx = 2 * (y * v[2] - z * v[1]);
  const ty = 2 * (z * v[0] - x * v[2]);
  const tz = 2 * (x * v[1] - y * v[0]);
  return [v[0] + w * tx + (y * tz - z * ty), v[1] + w * ty + (z * tx - x * tz), v[2] + w * tz + (x * ty - y * tx)];
}

/** The box's 12 edges as 24 world points (line segments), like
 *  awcd_visualizer's _bbox_line_list_points with the marker pose applied. */
export function boxEdges(center: number[], dims: number[], quat: number[]): [number, number, number][] {
  const n = Math.hypot(quat[0], quat[1], quat[2], quat[3]);
  const q = n > 1e-9 ? quat.map(v => v / n) : [0, 0, 0, 1];
  const h = dims.map(d => Math.max(d, 0.05) / 2);
  const corner = (i: number) => rot(q, [(i & 1 ? 1 : -1) * h[0], (i & 2 ? 1 : -1) * h[1], (i & 4 ? 1 : -1) * h[2]]);
  const c = Array.from({length: 8}, (_, i) => corner(i));
  const edges = [[0, 1], [2, 3], [4, 5], [6, 7], [0, 2], [1, 3], [4, 6], [5, 7], [0, 4], [1, 5], [2, 6], [3, 7]];
  const out: [number, number, number][] = [];
  for (const [a, b] of edges)
    for (const k of [a, b]) out.push([center[0] + c[k][0], center[1] + c[k][1], center[2] + c[k][2]]);
  return out;
}

/** Where the label floats: above the box (awcd_visualizer: top + text scale). */
export function labelAt(c: Pick<AwcdChange, "center" | "dims">): [number, number, number] {
  return [c.center[0], c.center[1], c.center[2] + c.dims[2] / 2 + 0.4];
}

/** "3 added · 1 removed" / "no changes" */
export function countsText(st: AwcdState | null): string {
  if (!st || !st.changes.length) return "no changes";
  return `${st.counts.added} added · ${st.counts.removed} removed`;
}
