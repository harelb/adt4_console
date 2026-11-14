// Global-frame alignment of a robot's live layers.
//
// A robot streams everything in its own scene-graph frame (the DSG header
// frame, e.g. `hamilton/odom`): live graph, mesh tiles, executor paths and
// target, and (when TF allows) the pose and occupancy grid. Prior maps (file /
// Neo4j sources) and the planner live in the global `map` frame. Once the robot
// is relocalized (hydra_multi / ROMAN loop closures publish `map -> <robot>/odom`)
// its fleetlink agent sends that transform in the C1 state as `frame_align`.
//
// Policy: the transform is applied to every layer of that robot whose frame is
// the alignment's child frame whenever `ok` is true, regardless of which other
// sources are shown (the view's fixed frame is always `map`, like RViz with
// `Fixed Frame: map`). `ok` false (not relocalized, no global frame) draws the
// layers in their native frame, as before, and the legend says so in amber.
// A layer already in the parent frame (e.g. a pose the agent could only resolve
// in `map`) is drawn as is; a layer in some other frame is drawn as is too.
import * as THREE from "three";
import type {FrameAlign, Pose, Reloc} from "../types";
import {nearestPlace, type GraphModel} from "./graphModel";

export type Vec3 = [number, number, number];

/** `live:<robot>` graph source -> robot name (null for prior-map sources). */
export function robotOfSource(srcId: string): string | null {
  return srcId.startsWith("live:") ? srcId.slice(5) || null : null;
}

function validT(T: unknown): T is number[] {
  return Array.isArray(T) && T.length === 7 && T.every(v => Number.isFinite(v));
}

/** parent <- child as a Matrix4, or null when there is no usable alignment. */
export function alignMatrix(fa: FrameAlign | null | undefined): THREE.Matrix4 | null {
  if (!fa || !fa.ok || !validT(fa.T)) return null;
  const [x, y, z, qw, qx, qy, qz] = fa.T;
  const q = new THREE.Quaternion(qx, qy, qz, qw);
  if (q.lengthSq() < 1e-12) return null;
  q.normalize();
  return new THREE.Matrix4().compose(new THREE.Vector3(x, y, z), q, new THREE.Vector3(1, 1, 1));
}

/** The matrix to draw a layer expressed in `frame` with (null = draw as is).
 *  An empty / missing frame is taken to be the scene-graph (child) frame. */
export function layerMatrix(fa: FrameAlign | null | undefined, frame?: string | null): THREE.Matrix4 | null {
  const m = alignMatrix(fa);
  if (!m || !fa) return null;
  if (frame && frame !== fa.child) return null; // parent frame (already global) or unknown
  return m;
}

/** Stable key of what `layerMatrix` would return (for memo / effect deps). */
export function layerKey(fa: FrameAlign | null | undefined, frame?: string | null): string {
  const m = layerMatrix(fa, frame);
  return m ? m.elements.map(v => v.toFixed(5)).join(",") : "";
}

export function applyPoint(m: THREE.Matrix4 | null, p: Vec3): Vec3 {
  if (!m) return p;
  const v = new THREE.Vector3(p[0], p[1], p[2]).applyMatrix4(m);
  return [v.x, v.y, v.z];
}

/** World (global frame) point -> the layer's own frame. */
export function invertPoint(m: THREE.Matrix4 | null, p: Vec3): Vec3 {
  if (!m) return p;
  const v = new THREE.Vector3(p[0], p[1], p[2]).applyMatrix4(m.clone().invert());
  return [v.x, v.y, v.z];
}

/** World ray -> the layer's own frame (origin transformed, direction rotated). */
export function invertRay(m: THREE.Matrix4 | null, o: Vec3, d: Vec3): {o: Vec3; d: Vec3} {
  if (!m) return {o, d};
  const inv = m.clone().invert();
  const vo = new THREE.Vector3(...o).applyMatrix4(inv);
  const vd = new THREE.Vector3(...d).transformDirection(inv);
  return {o: [vo.x, vo.y, vo.z], d: [vd.x, vd.y, vd.z]};
}

/** A robot pose in the global frame (position and orientation), when aligned. */
export function poseInGlobal(pose: Pose, fa: FrameAlign | null | undefined): Pose {
  const m = layerMatrix(fa, pose.frame);
  if (!m) return pose;
  const local = new THREE.Matrix4().compose(
    new THREE.Vector3(pose.x, pose.y, pose.z),
    new THREE.Quaternion(pose.qx ?? 0, pose.qy ?? 0, pose.qz ?? 0, pose.qw ?? 1).normalize(),
    new THREE.Vector3(1, 1, 1),
  );
  const world = m.clone().multiply(local);
  const p = new THREE.Vector3();
  const q = new THREE.Quaternion();
  world.decompose(p, q, new THREE.Vector3());
  return {...pose, x: p.x, y: p.y, z: p.z, qw: q.w, qx: q.x, qy: q.y, qz: q.z, frame: fa!.parent};
}

export interface AlignStatus {
  tone: "ok" | "warn" | "err" | "none";
  text: string;
  title: string;
}

function fmtAge(s: number): string {
  return s < 90 ? `${s.toFixed(s < 10 ? 1 : 0)} s` : s < 5400 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1)} h`;
}

/** Legend / Live-panel status. `sinceStateS` = seconds since that state arrived
 *  (added to the robot-side `age_s`). */
export function alignStatus(fa: FrameAlign | null | undefined, sinceStateS = 0): AlignStatus {
  if (!fa) return {tone: "none", text: "", title: "this robot's agent does not report a global-frame alignment"};
  if (!fa.ok || !validT(fa.T)) {
    return {
      tone: "warn",
      text: `${fa.child || "odom"} frame — not relocalized`,
      title: `no ${fa.parent} -> ${fa.child} transform: the live layers are drawn in the robot's own frame${fa.reason ? `\n${fa.reason}` : ""}`,
    };
  }
  if (fa.parent === fa.child) return {tone: "ok", text: `in ${fa.parent} frame`, title: `the robot's scene graph is published in ${fa.parent}`};
  const [x, y, z] = fa.T;
  const age = fa.age_s === null || fa.age_s === undefined || !fa.stamp ? null : fa.age_s + Math.max(0, sinceStateS);
  return {
    tone: "ok",
    text: `aligned to ${fa.parent} (relocalized)${age === null ? " · static" : ` · ${fmtAge(age)}`}`,
    title: `${fa.parent} -> ${fa.child}: t = (${x.toFixed(2)}, ${y.toFixed(2)}, ${z.toFixed(2)}) m, yaw ${((quatYaw(fa.T) * 180) / Math.PI).toFixed(1)}°${age === null ? " (static transform)" : `, transform ${fmtAge(age)} old`}`,
  };
}

/** A correction this big is a relocalization (or a wrong one), not drift. */
export const BIG_JUMP_M = 3;
export const BIG_JUMP_DEG = 30;
/** A robot pose this far from every prior place is outside the prior map. */
export const OUTSIDE_PRIOR_M = 5;

export function isBigJump(j: {dxy: number; dyaw: number}): boolean {
  return j.dxy > BIG_JUMP_M || Math.abs(j.dyaw) > BIG_JUMP_DEG;
}

const plural = (n: number, w: string) => `${n} ${w}${n === 1 ? "" : "es"}`;

/** One row of a robot's ros_system_monitor table (C1 ``health.nodes``). */
export interface HealthNode {
  nickname?: string;
  node_name?: string;
  status?: string;
  notes?: string;
}

/** What the reloc status can say beyond the agent's ``reloc``: the prior map
 *  it matches against (as the console registered it, e.g. "Nov11_Building45_v2
 *  (euclid)": the prior's experiment and the robot that recorded it), the
 *  robot's own ROMAN loop-closure / hydra_multi monitor rows, and the robot's
 *  name (for "no signal from <robot>"; default: the alignment's child frame). */
export interface RelocContext {
  prior?: string | null;
  roman?: HealthNode | null;
  hydraMulti?: HealthNode | null;
  robot?: string;
}

/** ``<robot>/<name>`` (or bare ``name``) in a C1 ``health`` table; null if the
 *  table lacks it, undefined when there is no table (nothing to say). */
export function healthNode(health: {nodes?: unknown} | null | undefined, robot: string, name: string): HealthNode | null | undefined {
  const nodes = health?.nodes;
  if (!Array.isArray(nodes)) return undefined;
  for (const n of nodes as HealthNode[]) {
    if (n && (n.nickname === `${robot}/${name}` || n.nickname === name)) return n;
  }
  return null;
}

/** hydra_multi's monitor note "N / M valid interrobot loop closures"
 *  (hydra_multi_ros status_monitor.cpp; backend routes/change_detection.py). */
const HYDRA_LC_RE = /(\d+)\s*\/\s*(\d+)\s+valid\s+inter-?robot\s+loop\s+closures/i;

/** Accepted / total inter-robot (prior-map) loop closures from the hydra_multi
 *  row; null when the row or its count is missing (stale: "time since last spin"). */
export function hydraMatches(node: HealthNode | null | undefined): {accepted: number; total: number} | null {
  const m = node?.notes ? HYDRA_LC_RE.exec(node.notes) : null;
  return m ? {accepted: Number(m[1]), total: Number(m[2])} : null;
}

/** A robot counts as relocalized once hydra_multi has accepted this many
 *  inter-robot (prior-map) loop closures for it (backend
 *  routes/change_detection.py RELOC_MIN_HYDRA_LCS). */
export const RELOC_MIN_HYDRA_LCS = 5;

/** "...; last optimization took 0.412 s, 12.3 s ago" (hydra_multi status note,
 *  Hydra feat/hydra-multi-status-optimization). */
const HYDRA_OPT_RE = /last\s+optimization\s+took\s+([\d.]+)\s*s\s*,\s*([\d.]+)\s*s\s+ago/i;

/** hydra_multi's last optimization: how long it took and how long before the
 *  note was published it finished; null when the note does not say (no
 *  optimization yet, or an older hydra_multi). */
export function hydraOptimization(node: HealthNode | null | undefined): {tookS: number; agoS: number} | null {
  const m = node?.notes ? HYDRA_OPT_RE.exec(node.notes) : null;
  return m ? {tookS: Number(m[1]), agoS: Number(m[2])} : null;
}

/** A ``map -> odom`` this far from the initial estimate is an alignment, not
 *  optimizer noise (backend MOVED_M / MOVED_DEG). */
export const MOVED_M = 0.5;
export const MOVED_DEG = 2;

/** How far ``map -> odom`` moved from the initial estimate (since the agent's
 *  first sample, ``reloc.total``, or from identity, hydra_multi's starting
 *  estimate: an agent started after the relocalization saw no move), or null. */
export function movedFromInitial(fa: FrameAlign, reloc: Reloc | null | undefined): string | null {
  const moves: [number, number][] = [];
  if (reloc?.total) moves.push([reloc.total.dxy, reloc.total.dyaw]);
  if (validT(fa.T)) moves.push([Math.hypot(fa.T[0], fa.T[1]), (quatYaw(fa.T) * 180) / Math.PI]);
  for (const [dxy, dyaw] of moves) {
    if (Math.abs(dxy) > MOVED_M || Math.abs(dyaw) > MOVED_DEG) return `${Math.abs(dxy).toFixed(1)} m / ${dyaw.toFixed(0)}°`;
  }
  return null;
}

/** The prior map's display name from a graph-source label ("prior: X" -> "X"). */
export function priorName(label: string | null | undefined): string | null {
  if (!label) return null;
  const name = label.replace(/^prior:\s*/, "").replace(/ \(([^()]+)\)$/, " (recorded by $1)");
  return name || null;
}

const ROMAN_UP = new Set(["nominal", "warning"]);
const nodeLine = (what: string, n: HealthNode | null | undefined) =>
  n ? `${what}: ${n.status ?? "?"}${n.notes ? ` — ${n.notes}` : ""}` : `${what}: not in the robot's status table`;

/** Legend / Live-panel status. Relocalized (green) means hydra_multi has
 *  accepted at least :data:`RELOC_MIN_HYDRA_LCS` inter-robot (prior-map) loop
 *  closures for the robot (its monitor row; the backend's ``relocalized()``
 *  uses the same rule), unless the placement is suspect (red: outside the prior
 *  map, or a second big correction: the first one is the relocalization
 *  itself). Fewer is amber "relocalizing · hydra 3/5 LCs" ("not relocalized"
 *  at 0). Without a hydra_multi count, fleetlink's ROMAN counter and a ``map ->
 *  odom`` move are shown (amber), but are not enough. ``distToPrior`` = metres
 *  from the robot to the nearest prior-map place (null: no prior shown / no
 *  pose). Agents with neither ``reloc`` nor a hydra_multi count keep
 *  :func:`alignStatus`. The text never names another robot: a prior map is
 *  named in the tooltip only. */
export function relocStatus(
  fa: FrameAlign | null | undefined,
  reloc: Reloc | null | undefined,
  distToPrior: number | null,
  sinceStateS = 0,
  ctx: RelocContext = {},
): AlignStatus {
  const base = alignStatus(fa, sinceStateS);
  const hm = hydraMatches(ctx.hydraMulti);
  if (!fa || fa.parent === fa.child || (!reloc && !hm)) return base;
  const need = RELOC_MIN_HYDRA_LCS;
  const enough = !!hm && hm.accepted >= need;
  const priorName = ctx.prior ? `prior map ${ctx.prior}` : "the prior map";
  const hydraLine = hm
    ? `hydra_multi accepted ${hm.accepted} / ${hm.total} inter-robot loop closures against ${priorName} (relocalized at ${need})`
    : ctx.hydraMulti
      ? `hydra_multi reports no loop-closure count (${ctx.hydraMulti.status ?? "?"}${ctx.hydraMulti.notes ? ` — ${ctx.hydraMulti.notes}` : ""})`
      : `hydra_multi: ${ctx.hydraMulti === null ? "not in the robot's status table" : "no status table from the robot"}`;
  if (base.tone !== "ok") {
    // matched, but this console has no usable transform yet: say both
    if (hm && enough) {
      return {tone: "ok", text: `relocalized · hydra ${hm.accepted} LCs · drawn in ${fa.child || "odom"}`, title: `${hydraLine}\n${base.title}`};
    }
    return base;
  }
  const since = Math.max(0, sinceStateS);
  const lcAge = !reloc || reloc.last_lc_age_s === null || reloc.last_lc_age_s === undefined ? null : reloc.last_lc_age_s + since;
  const priorIds = [...new Set((reloc?.lcs ?? []).map(lc => lc.prior))].sort((a, b) => a - b);
  const lines = [
    hydraLine,
    ...(reloc
      ? [
          `fleetlink saw ${reloc.prior_lcs} ROMAN loop closures against ${priorName}${reloc.prior_lcs ? ` (best ${reloc.best_assoc} objects, last ${fmtAge(lcAge ?? 0)} ago${priorIds.length ? `, ROMAN prior id ${priorIds.join(", ")}` : ""})` : ""}`,
          `${fa.parent} -> ${fa.child} moved ${reloc.jumps} times` +
            (reloc.total ? `, in all ${reloc.total.dxy.toFixed(1)} m / ${reloc.total.dyaw.toFixed(0)}°` : ""),
        ]
      : []),
    distToPrior === null ? "prior map not shown: cannot check the robot is inside it" : `nearest prior place ${distToPrior.toFixed(1)} m away`,
    ...(ctx.roman !== undefined ? [nodeLine("ROMAN loop closures (roman_lc)", ctx.roman)] : []),
    ...(ctx.hydraMulti && ctx.hydraMulti.status && !ROMAN_UP.has(ctx.hydraMulti.status) ? [nodeLine("hydra_multi", ctx.hydraMulti)] : []),
    base.title,
  ];
  const title = lines.join("\n");
  const big = reloc ? (reloc.recent_jumps ?? (reloc.last_jump ? [reloc.last_jump] : [])).filter(isBigJump).length : 0;
  const suspect = (): AlignStatus | null => {
    if (distToPrior !== null && distToPrior > OUTSIDE_PRIOR_M) {
      return {tone: "err", text: `suspect — ${distToPrior.toFixed(0)} m outside the prior map`, title: `placed outside the prior map: a wrong loop closure is likely\n${title}`};
    }
    if (big >= 2) {
      return {tone: "err", text: `suspect — placement jumped ${big}×`, title: `${big} large corrections (> ${BIG_JUMP_M} m or ${BIG_JUMP_DEG}°): loop closures disagree\n${title}`};
    }
    return null;
  };
  if (hm && enough) {
    return suspect() ?? {tone: "ok", text: `relocalized · hydra ${hm.accepted} LCs`, title};
  }
  if (hm && hm.accepted > 0) {
    return {
      tone: "warn",
      text: `relocalizing · hydra ${hm.accepted}/${need} LCs`,
      title: `relocalizing: hydra_multi accepted ${hm.accepted} loop closures against ${priorName}, needs ${need}\n${title}`,
    };
  }
  if (hm) {
    const matching = !!ctx.roman?.status && ROMAN_UP.has(ctx.roman.status);
    return {
      tone: "warn",
      text: matching ? "not relocalized · matching prior map…" : "not relocalized · no prior-map match yet",
      title: `not relocalized: no loop closure against ${priorName} accepted yet, so ${fa.parent} -> ${fa.child} is only the initial estimate, not a match\n${title}`,
    };
  }
  if (reloc && reloc.prior_lcs > 0) {
    return {
      tone: "warn",
      text: `not confirmed · no hydra count · ROMAN ${plural(reloc.prior_lcs, "match")}`,
      title: `not confirmed: fleetlink saw ROMAN prior-map matches, but hydra_multi reports no loop-closure count (relocalized needs ${need} accepted by hydra_multi)\n${title}`,
    };
  }
  const moved = movedFromInitial(fa, reloc);
  if (moved) {
    return {
      tone: "warn",
      text: "aligned · no hydra count",
      title: `aligned: ${fa.parent} -> ${fa.child} moved ${moved} from the initial estimate, but hydra_multi reports no loop-closure count (relocalized needs ${need})\n${title}`,
    };
  }
  const robot = ctx.robot || fa.child.split("/")[0] || "the robot";
  return {
    tone: "warn",
    text: `relocalization unknown · no signal from ${robot}`,
    title: `unknown (no relocalization signal from ${robot}): no hydra_multi loop-closure count, no ROMAN prior-map match seen by fleetlink, and ${fa.parent} -> ${fa.child} is still at the initial estimate\n${title}`,
  };
}

function quatYaw(T: number[]): number {
  const [, , , qw, qx, qy, qz] = T;
  return Math.atan2(2 * (qw * qz + qx * qy), 1 - 2 * (qy * qy + qz * qz));
}

/** Goto picking: the place nearest a clicked global-frame point across the
 *  shown sources, each searched in its own frame (`matrixOf(id)` = that source's
 *  drawing matrix; live sources: the robot alignment). omniplanner's goto / TSP
 *  goals are node symbols resolved in the planner's own scene graph, so no
 *  coordinates go to the planner; only the snapping needs the conversion. */
export function nearestGotoPlace(
  models: {id: string; model: GraphModel}[],
  p: Vec3,
  matrixOf: (id: string) => THREE.Matrix4 | null,
  maxDist = 15,
): {src: string; sym: string; d: number; pos: Vec3} | null {
  let best: {src: string; sym: string; d: number; pos: Vec3} | null = null;
  for (const {id, model} of models) {
    const m = matrixOf(id);
    const local = invertPoint(m, p);
    const n = nearestPlace(model, local, {maxDist});
    if (!n) continue;
    const d = Math.hypot(n.pos[0] - local[0], n.pos[1] - local[1]);
    if (!best || d < best.d) best = {src: id, sym: n.sym, d, pos: applyPoint(m, n.pos)};
  }
  return best;
}

/** Snap a goto pick (global frame) to a node of the planner's own map.
 *  The planner refuses a symbol its map lacks ("not in scene graph"), and a
 *  symbol picked on another source (a live mirror's agent pose, a prior file)
 *  can be missing there. `object` (the pick was an object) first tries a
 *  planner object within `objectDist`, else the nearest planner place. */
export function snapToPlannerMap(
  planner: {id: string; model: GraphModel},
  p: Vec3,
  matrixOf: (id: string) => THREE.Matrix4 | null,
  opts: {object?: boolean; objectDist?: number; maxDist?: number} = {},
): {src: string; sym: string; d: number; pos: Vec3} | null {
  const m = matrixOf(planner.id);
  const local = invertPoint(m, p);
  const n =
    (opts.object ? nearestPlace(planner.model, local, {kinds: ["objects"], maxDist: opts.objectDist ?? 1}) : null) ??
    nearestPlace(planner.model, local, {maxDist: opts.maxDist ?? 15});
  if (!n) return null;
  return {src: planner.id, sym: n.sym, d: Math.hypot(n.pos[0] - local[0], n.pos[1] - local[1]), pos: applyPoint(m, n.pos)};
}
