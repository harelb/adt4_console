// Canvas legend: one chip per live robot (or robot with a plan) in its colour,
// so glyphs, plans and executor paths can be told apart at a glance. Each live
// robot's chip also says whether its live layers are aligned to the global map
// frame (relocalized) or drawn in the robot's own odom frame (amber).
// The focused robot's chip is highlighted; clicking a chip focuses that robot
// (clicking the focused one goes back to all robots).
import {useMemo} from "react";
import {frameAlignOf, useLiveRobots, useRobots} from "../stores/robots";
import {useRobotColorOf} from "../lib/palette";
import {healthNode, poseInGlobal, priorName, relocStatus, robotOfSource, type RelocContext} from "../lib/frameAlign";
import {nearestPlace} from "../lib/graphModel";
import {useViewer} from "../stores/viewer";
import {useFocusedRobot} from "./useRobotFocus";
import {PlanExecStatus} from "./PlanExecStatus";
import {PhoenixChips} from "./PhoenixStatus";
import type {FrameAlign, Pose} from "../types";
import {useManipulation} from "../stores/manipulation";

/** A robot waiting for a grasp decision: a badge on its chip that opens the approval panel. */
export function GraspChip({robot}: {robot: string}) {
  const n = useManipulation(s => s.pending.filter(p => p.robot === robot).length);
  if (!n) return null;
  return (
    <button
      type="button"
      className="btn btn-xs btn-primary grasp-chip is-pulsing"
      data-testid={`grasp-chip-${robot}`}
      title={`${robot} is waiting for a grasp decision: open the approval panel`}
      onClick={e => {
        e.stopPropagation();
        useManipulation.getState().open({robot});
      }}
      onKeyDown={e => e.stopPropagation()}
    >
      Grasp approval{n > 1 ? ` (${n})` : ""}
    </button>
  );
}

/** Metres from the robot (global frame) to the nearest place of a shown prior
 *  map (every enabled non-live source); null when none is shown or no pose. */
export function useDistToPrior(robot: string, pose: Pose | undefined, fa: FrameAlign | undefined): number | null {
  const sources = useViewer(s => s.sources);
  const order = useViewer(s => s.order);
  if (!pose || !fa?.ok) return null;
  const p = poseInGlobal(pose, fa);
  if (p.frame !== fa.parent) return null;
  let best: number | null = null;
  for (const id of order) {
    const src = sources[id];
    if (!src?.enabled || !src.model || robotOfSource(id) !== null) continue;
    const n = nearestPlace(src.model, [p.x, p.y, p.z]);
    if (!n) continue;
    const d = Math.hypot(n.pos[0] - p.x, n.pos[1] - p.y);
    if (best === null || d < best) best = d;
  }
  return best;
}

/** The prior map the robots relocalize against (the registered prior source,
 *  "file:prior:<name>", preferring a shown one) and the robot's ROMAN /
 *  hydra_multi monitor rows, for :func:`relocStatus`. */
export function useRelocContext(robot: string): RelocContext {
  const sources = useViewer(s => s.sources);
  const order = useViewer(s => s.order);
  const health = useRobots(s => s.state[robot]?.health);
  return useMemo(() => {
    const priors = order.filter(id => id.startsWith("file:prior:") && sources[id]);
    const id = priors.find(i => sources[i]?.enabled) ?? priors[0];
    return {
      prior: id ? priorName(sources[id]?.label || id.slice("file:".length)) : null,
      roman: healthNode(health, robot, "roman_lc"),
      hydraMulti: healthNode(health, robot, "hydra_multi"),
      robot,
    };
  }, [sources, order, health, robot]);
}

/** "relocalized · 44 accepted matches" (hydra_multi) / "relocalized · 4 matches · last
 *  20 s ago" (ROMAN via fleetlink) / "aligned · no match count" / "relocalization
 *  unknown · no signal from euclid" / "not relocalized · matching prior map…" /
 *  "not relocalized · no prior-map match yet" (old wording "no prior match yet —
 *  starting guess" was read as "starting gauss") /
 *  "suspect — 13 m outside the prior map" (older agents: the
 *  transform-only "aligned to map (relocalized)"). */
export function AlignStatus({robot, className = ""}: {robot: string; className?: string}) {
  const fa = useRobots(s => frameAlignOf(robot, s));
  const reloc = useRobots(s => s.state[robot]?.reloc ?? s.link[robot]?.reloc);
  const pose = useRobots(s => s.state[robot]?.pose);
  const at = useRobots(s => s.stateAt[robot]);
  const dist = useDistToPrior(robot, pose, fa);
  const ctx = useRelocContext(robot);
  const st = relocStatus(fa, reloc, dist, at ? (Date.now() - at) / 1000 : 0, ctx);
  if (st.tone === "none") return null;
  return (
    <span className={`align-status tone-${st.tone} ${className}`} title={st.title} data-testid={`align-${robot}`}>
      {st.text}
    </span>
  );
}

export function RobotLegend() {
  const live = useLiveRobots();
  const plans = useRobots(s => s.plan);
  const colorOf = useRobotColorOf();
  const focused = useFocusedRobot();
  const setFocus = useViewer(s => s.setFocus);
  const robots = useMemo(() => [...new Set([...live, ...Object.keys(plans)])].sort(), [live, plans]);
  if (!robots.length) return null;
  return (
    <div className="robot-legend" aria-label="Robot colours">
      {robots.map(r => (
        <span
          key={r}
          role="button"
          tabIndex={0}
          aria-pressed={r === focused}
          className={`robot-chip ${r === focused ? "focused" : focused ? "dim" : ""}`}
          style={{borderColor: colorOf(r), ...(r === focused ? {boxShadow: `0 0 0 1px ${colorOf(r)}`} : {})}}
          title={r === focused ? "Focused · click for all robots" : `Focus ${r}`}
          onClick={() => setFocus(r === focused ? "all" : r)}
          onKeyDown={e => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              setFocus(r === focused ? "all" : r);
            }
          }}
        >
          <span className="swatch" style={{background: colorOf(r)}} />
          {r}
          <PhoenixChips robot={r} />
          <AlignStatus robot={r} />
          <PlanExecStatus robot={r} />
          <GraspChip robot={r} />
        </span>
      ))}
    </div>
  );
}
