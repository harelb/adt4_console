// "Clear paths": hide plan / executor-path drawings on the map, in this console
// only (stores/clearedPaths.ts). A new plan or path draws again.
import {usePlanPreview} from "../stores/planPreview";
import {useRobots} from "../stores/robots";
import {hasDrawn, planCleared, pathsCleared, useClearedPaths} from "../stores/clearedPaths";

const TITLE = "Hide every robot's plan and path drawings on the map (console only: nothing is sent to the robots or the planner). A new plan or path is drawn again.";

/** Robots with a plan or path drawn right now. */
function useDrawnRobots(): string[] {
  const plan = useRobots(s => s.plan);
  const paths = useRobots(s => s.paths);
  const plans = useClearedPaths(s => s.plans);
  const cpaths = useClearedPaths(s => s.paths);
  const robots = [...new Set([...Object.keys(plan), ...Object.keys(paths)])];
  return robots.filter(r => hasDrawn(r, {plan, paths}, {plans, paths: cpaths}));
}

/** All robots: the viewer toolbar's button. */
export function ClearPathsButton({compact = false}: {compact?: boolean}) {
  const drawn = useDrawnRobots();
  const preview = usePlanPreview(s => s.preview);
  const clearAll = useClearedPaths(s => s.clearAll);
  const none = drawn.length === 0 && !preview;
  return (
    <button className="btn btn-sm panel-btn" disabled={none} onClick={clearAll} title={none ? "No plan or path drawn" : TITLE} aria-label="Clear paths" data-testid="clear-paths">
      {compact ? "Clear" : "Clear paths"}
    </button>
  );
}

/** Display panel row: clear all, and bring cleared ones back. */
export function ClearPathsRow() {
  const drawn = useDrawnRobots();
  const preview = usePlanPreview(s => s.preview);
  const plans = useClearedPaths(s => s.plans);
  const cpaths = useClearedPaths(s => s.paths);
  const {clearAll, restore} = useClearedPaths.getState();
  const plan = useRobots(s => s.plan);
  const paths = useRobots(s => s.paths);
  // cleared drawings that would come back (the robot still has them)
  const hidden = [...new Set([...Object.keys(plans), ...Object.keys(cpaths)])].filter(
    r => planCleared({plans}, r, plan[r]) || pathsCleared({paths: cpaths}, r, paths[r]),
  );
  return (
    <div className="layout-row">
      <span className="muted small">Plans & paths on the map</span>
      <span className="row gap-sm">
        <button className="btn btn-xs" disabled={!drawn.length && !preview} onClick={clearAll} title={TITLE} data-testid="clear-paths-all">
          Clear all
        </button>
        <button className="btn btn-xs" disabled={!hidden.length} onClick={() => hidden.forEach(restore)} title="Draw the cleared plans and paths again" data-testid="restore-paths">
          Show cleared{hidden.length ? ` (${hidden.length})` : ""}
        </button>
      </span>
    </div>
  );
}

/** One robot (Plan tab): clear its drawing, or show it again. */
export function ClearRobotPaths({robot}: {robot: string}) {
  const plan = useRobots(s => s.plan[robot]);
  const paths = useRobots(s => s.paths[robot]);
  const plans = useClearedPaths(s => s.plans);
  const cpaths = useClearedPaths(s => s.paths);
  const {clear, restore} = useClearedPaths.getState();
  const isHidden = planCleared({plans}, robot, plan) || pathsCleared({paths: cpaths}, robot, paths);
  const drawn = hasDrawn(robot, {plan: plan ? {[robot]: plan} : {}, paths: paths ? {[robot]: paths} : {}}, {plans, paths: cpaths});
  if (!drawn && !isHidden) return null;
  return drawn ? (
    <button className="btn btn-sm" onClick={() => clear(robot)} title={`Hide ${robot}'s plan and path on the map (console only). Its next plan is drawn again.`} data-testid={`clear-paths-${robot}`}>
      Clear path
    </button>
  ) : (
    <button className="btn btn-sm" onClick={() => restore(robot)} title={`Draw ${robot}'s cleared plan and path again`} data-testid={`restore-paths-${robot}`}>
      Show path
    </button>
  );
}
