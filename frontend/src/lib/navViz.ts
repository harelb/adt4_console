// On-demand navigation overlays (server: adt4_console/nav_viz.py):
// `robot/<r>/nav_viz` carries the Husky's MPPI / mid-range planner markers, as
// in phoenix-r2's gauss_mppi_debug.rviz (MPPI plan, reference, rollouts,
// mid-range plan), each layer in its own frame (usually `<robot>/odom`).
// Pure parsing / formatting here; stores/navViz.ts holds the state and leases,
// viewer/NavVizOverlay.tsx draws it.

export type Vec3 = [number, number, number];

export interface NavMarker {
  ns: string;
  id: number;
  kind: "strip" | "segments" | "points";
  pts: Vec3[];
  color: [number, number, number, number] | null;
}

export interface NavLayer {
  label: string;
  frame: string;
  t: number;
  markers: NavMarker[];
}

export interface NavVizPayload {
  layers: Record<string, NavLayer>;
  t: number;
}

/** Layer keys in draw order, with how each is drawn (RViz-like). */
export const NAV_LAYERS: {key: string; label: string; width: number; opacity: number; shade: number; dashed?: boolean}[] = [
  {key: "srp_debug", label: "MPPI rollouts", width: 1, opacity: 0.35, shade: 1.3},
  {key: "mrp_plan", label: "mid-range plan", width: 2.5, opacity: 0.9, shade: 0.7, dashed: true},
  {key: "srp_reference", label: "MPPI reference", width: 2, opacity: 0.9, shade: 1.5},
  {key: "srp_plan", label: "MPPI plan", width: 3.5, opacity: 1, shade: 1},
];

const finite = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);

function vec3(p: unknown): Vec3 | null {
  return Array.isArray(p) && p.length >= 3 && finite(p[0]) && finite(p[1]) && finite(p[2]) ? [p[0], p[1], p[2]] : null;
}

/** Validate a `robot/<r>/nav_viz` payload; malformed markers / points are dropped. */
export function parseNavViz(d: unknown): NavVizPayload | null {
  if (!d || typeof d !== "object") return null;
  const raw = (d as {layers?: unknown}).layers;
  if (!raw || typeof raw !== "object") return null;
  const layers: Record<string, NavLayer> = {};
  for (const [k, v] of Object.entries(raw as Record<string, unknown>)) {
    if (!v || typeof v !== "object") continue;
    const l = v as Partial<NavLayer> & {markers?: unknown};
    const markers: NavMarker[] = [];
    for (const m of Array.isArray(l.markers) ? l.markers : []) {
      if (!m || typeof m !== "object") continue;
      const mm = m as Partial<NavMarker>;
      if (mm.kind !== "strip" && mm.kind !== "segments" && mm.kind !== "points") continue;
      const pts = (Array.isArray(mm.pts) ? mm.pts : []).map(vec3).filter((p): p is Vec3 => p !== null);
      if (!pts.length) continue;
      const c = Array.isArray(mm.color) && mm.color.length === 4 && mm.color.every(finite) ? (mm.color as NavMarker["color"]) : null;
      markers.push({ns: String(mm.ns ?? ""), id: Number(mm.id ?? 0), kind: mm.kind, pts, color: c});
    }
    layers[k] = {label: String(l.label ?? k), frame: String(l.frame ?? ""), t: finite(l.t) ? l.t : 0, markers};
  }
  return {layers, t: finite((d as {t?: unknown}).t) ? (d as {t: number}).t : 0};
}

/** Segment pairs for a `segments` marker, consecutive pairs for a strip. */
export function markerSegments(m: NavMarker): Vec3[] {
  if (m.kind === "segments") return m.pts;
  const out: Vec3[] = [];
  for (let i = 1; i < m.pts.length; i++) out.push(m.pts[i - 1], m.pts[i]);
  return out;
}
