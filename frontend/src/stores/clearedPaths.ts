// Operator "clear paths" for the map (console only: nothing is sent to a robot
// or the planner). Plans arrive latched (omniplanner compiled_plan_out), so a
// finished plan's drawing would otherwise stay forever, across reloads and
// console restarts. Clearing remembers WHAT was drawn (each robot's plan_id and
// its executor paths' fingerprint); a new plan (another plan_id) or a new path
// draws again. The remembered ids live per browser (localStorage).
import {create} from "zustand";
import type {RobotPaths} from "../lib/overlays";
import type {RobotPlan} from "../types";
import {useRobots} from "./robots";
import {usePlanPreview} from "./planPreview";

const KEY = "adt4.clearedPaths";

interface Cleared {
  /** robot -> the plan_id cleared */
  plans: Record<string, string>;
  /** robot -> the executor paths' fingerprint cleared */
  paths: Record<string, string>;
}

/** Executor paths' identity: the robot's hash, else the geometry (not the stamp:
 *  a latched path republished unchanged is the same path). */
export function pathsKey(p: RobotPaths | null | undefined): string | null {
  if (!p) return null;
  if (p.hash) return `h:${p.hash}`;
  const r = (v: number[]) => v.map(x => Math.round(x * 100) / 100).join(",");
  return `g:${p.frame}|${p.smooth.map(r).join(";")}|${p.mlp.map(r).join(";")}|${p.target ? r(p.target) : ""}`;
}

export function planKey(p: RobotPlan | null | undefined): string | null {
  return p?.plan?.plan_id ?? null;
}

function load(): Cleared {
  try {
    const raw = localStorage.getItem(KEY);
    const d = raw ? (JSON.parse(raw) as Partial<Cleared>) : null;
    const obj = (v: unknown) => (v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, string>) : {});
    return {plans: obj(d?.plans), paths: obj(d?.paths)};
  } catch {
    return {plans: {}, paths: {}};
  }
}

function save(c: Cleared) {
  try {
    localStorage.setItem(KEY, JSON.stringify(c));
  } catch {
    /* private window / blocked storage: the in-memory choice holds this session */
  }
}

interface ClearedStore extends Cleared {
  /** hide `robot`'s current plan and executor paths (nothing is sent anywhere) */
  clear: (robot: string) => void;
  /** every robot's, and the plan preview (a direct route / PDDL preview) */
  clearAll: () => void;
  /** show `robot`'s cleared drawings again */
  restore: (robot: string) => void;
  /** re-read localStorage (tests) */
  reload: () => void;
}

export const useClearedPaths = create<ClearedStore>((set, get) => ({
  ...load(),
  clear: robot => {
    const r = useRobots.getState();
    const plans = {...get().plans};
    const paths = {...get().paths};
    const pk = planKey(r.plan[robot]);
    const xk = pathsKey(r.paths[robot]);
    if (pk) plans[robot] = pk;
    if (xk) paths[robot] = xk;
    save({plans, paths});
    set({plans, paths});
  },
  clearAll: () => {
    const r = useRobots.getState();
    const plans = {...get().plans};
    const paths = {...get().paths};
    for (const [robot, p] of Object.entries(r.plan)) {
      const k = planKey(p);
      if (k) plans[robot] = k;
    }
    for (const [robot, p] of Object.entries(r.paths)) {
      const k = pathsKey(p);
      if (k) paths[robot] = k;
    }
    save({plans, paths});
    set({plans, paths});
    // a previewed (not sent) plan's routes are console-only too
    if (usePlanPreview.getState().preview) usePlanPreview.getState().set(null, null);
  },
  restore: robot => {
    const plans = {...get().plans};
    const paths = {...get().paths};
    delete plans[robot];
    delete paths[robot];
    save({plans, paths});
    set({plans, paths});
  },
  reload: () => set(load()),
}));

/** Is `robot`'s plan drawing hidden (cleared, and no new plan since)? */
export function planCleared(c: Pick<Cleared, "plans">, robot: string, plan: RobotPlan | null | undefined): boolean {
  const k = planKey(plan);
  return k !== null && c.plans[robot] === k;
}

export function pathsCleared(c: Pick<Cleared, "paths">, robot: string, paths: RobotPaths | null | undefined): boolean {
  const k = pathsKey(paths);
  return k !== null && c.paths[robot] === k;
}

/** Does `robot` have a plan or executor path drawn right now? */
export function hasDrawn(
  robot: string,
  s: {plan: Record<string, RobotPlan>; paths: Record<string, RobotPaths>} = useRobots.getState(),
  c: Cleared = useClearedPaths.getState(),
): boolean {
  const plan = s.plan[robot];
  const paths = s.paths[robot];
  return (!!plan?.plan?.actions?.length && !planCleared(c, robot, plan)) || (!!paths && !pathsCleared(c, robot, paths));
}
