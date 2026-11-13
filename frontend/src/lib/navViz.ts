// On-demand navigation overlays (server: adt4_console/nav_viz.py):
//  * `robot/<r>/nav_viz` - the Husky's MPPI / mid-range planner markers, as in
//    phoenix-r2's gauss_mppi_debug.rviz (MPPI plan, reference, rollouts,
//    mid-range plan), each layer in its own frame (usually `<robot>/odom`);
//  * `robot/<r>/gps` - the GPS fix (NavSatFix) with its map x/y.
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

export interface GpsFix {
  lat: number;
  lon: number;
  alt: number | null;
  status: number;
  status_text: string;
  service: number;
  /** 1-sigma east, north, up (m); null when the covariance is unknown */
  std: [number, number, number] | null;
  cov_type: string;
  frame: string;
  stamp: number | null;
  /** map x/y, null when the console has no GPS anchor / ADT4_ORIGIN_UTM */
  map: [number, number] | null;
  /** anchor = the map's saved GPS anchor, origin_utm = ADT4_ORIGIN_UTM, live = the live estimate */
  map_via: "anchor" | "origin_utm" | "live" | null;
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

/** Validate a `robot/<r>/gps` payload. */
export function parseGps(d: unknown): GpsFix | null {
  if (!d || typeof d !== "object") return null;
  const g = d as Partial<GpsFix>;
  if (!finite(g.lat) || !finite(g.lon)) return null;
  const map = Array.isArray(g.map) && g.map.length >= 2 && finite(g.map[0]) && finite(g.map[1]) ? ([g.map[0], g.map[1]] as [number, number]) : null;
  const std = Array.isArray(g.std) && g.std.length === 3 && g.std.every(finite) ? (g.std as [number, number, number]) : null;
  return {
    lat: g.lat,
    lon: g.lon,
    alt: finite(g.alt) ? g.alt : null,
    status: finite(g.status) ? g.status : -1,
    status_text: String(g.status_text ?? ""),
    service: finite(g.service) ? g.service : 0,
    std,
    cov_type: String(g.cov_type ?? "unknown"),
    frame: String(g.frame ?? ""),
    stamp: finite(g.stamp) ? g.stamp : null,
    map,
    map_via: g.map_via === "anchor" || g.map_via === "origin_utm" || g.map_via === "live" ? g.map_via : null,
    t: finite(g.t) ? g.t : 0,
  };
}

/** Horizontal 1-sigma (m): the larger of east / north, null when unknown. */
export const horizSigma = (g: GpsFix): number | null => (g.std ? Math.max(g.std[0], g.std[1]) : null);

/** Fix quality tone: RTK / SBAS with small sigma is good, no fix is bad. */
export function gpsTone(g: GpsFix): "ok" | "warn" | "err" {
  if (g.status < 0) return "err";
  const s = horizSigma(g);
  if (g.status >= 2 && (s === null || s < 0.5)) return "ok";
  return s !== null && s > 5 ? "err" : "warn";
}

/** "RTK fix · ±0.12 m · 3 s old" style text for labels and tooltips. */
export function gpsLabel(g: GpsFix, nowS: number = Date.now() / 1000): string {
  const s = horizSigma(g);
  const age = g.t ? Math.max(0, nowS - g.t) : null;
  return [
    g.status_text || `status ${g.status}`,
    s !== null ? `±${s < 1 ? s.toFixed(2) : s.toFixed(1)} m` : `σ ${g.cov_type}`,
    age !== null && age >= 2 ? `${age < 90 ? Math.round(age) + " s" : Math.round(age / 60) + " min"} old` : null,
  ]
    .filter(Boolean)
    .join(" · ");
}

/** Metres between the GPS fix and the robot's relocalized (map) x/y. */
export function gpsOffset(g: GpsFix | null | undefined, pose: {x: number; y: number} | null | undefined): number | null {
  if (!g?.map || !pose) return null;
  return Math.hypot(g.map[0] - pose.x, g.map[1] - pose.y);
}

/** Segment pairs for a `segments` marker, consecutive pairs for a strip. */
export function markerSegments(m: NavMarker): Vec3[] {
  if (m.kind === "segments") return m.pts;
  const out: Vec3[] = [];
  for (let i = 1; i < m.pts.length; i++) out.push(m.pts[i - 1], m.pts[i]);
  return out;
}
