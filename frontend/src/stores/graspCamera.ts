// Auto hand camera during a Spot grasp (per-browser setting, default on).
//
// When the operator approves a manipulation request (a grasp point selected),
// the camera views of that robot (Camera panel, drive full screen) show the
// HAND camera as live video until the grasp is done:
//   * the robot's next PICK / DRAG action_result (any terminal status),
//   * a new manipulation_request from that robot (the approval dialog takes over),
//   * the lease is lost (owned true -> false while the grasp runs),
//   * GRASP_TIMEOUT_MS after the approval,
//   * the operator changes the camera or mode, or presses "back" (manual wins).
// The views never touch their own camera / mode state while overriding, so
// ending the session shows the operator's previous choice again.
//
// Walk To does not switch: the arm is stowed while the robot walks, and it asks
// again with new images, so the current camera stays (an active session for
// that robot ends). Rejections end an active session too.
// A robot without a "hand" camera (no arm) is left alone.
// The robot is focused (unless a full-screen drive is open) so its camera view
// is the one shown; the old focus comes back at the end unless the operator
// changed it or took over the camera.
import {create} from "zustand";
import type {ActionResult, RobotPlan} from "../types";
import {useRobots} from "./robots";
import {useViewer} from "./viewer";

export const GRASP_CAM = "hand";
export const GRASP_TIMEOUT_MS = 90_000;
/** Hand-camera stream while grasping: smooth enough to watch the gripper, light on the radio. */
export const GRASP_TUNE = {fps: 10, kbps: 400, width: 640};
const KEY = "adt4.graspHandCamera";

export type GraspAnswer = "approve" | "walk" | "reject";
export type GraspEndReason = "result" | "new_request" | "lease" | "timeout" | "manual" | "back" | "walk_to" | "reject" | "disabled";

export interface GraspSession {
  robot: string;
  requestId: string;
  startedAt: number;
  deadline: number;
  /** results already on the robot's plan when the grasp was approved */
  baseline: string[];
  /** the lease was seen owned during this grasp (a later "not owned" ends it) */
  leaseOwned: boolean;
  /** focus before we focused the robot (null: we did not change it) */
  prevFocus: string | null;
}

export function loadGraspEnabled(): boolean {
  try {
    return localStorage.getItem(KEY) !== "off";
  } catch {
    return true;
  }
}

function saveGraspEnabled(on: boolean) {
  try {
    localStorage.setItem(KEY, on ? "on" : "off");
  } catch {
    /* private window: keep the in-memory choice */
  }
}

const resultKey = (r: ActionResult) => JSON.stringify([r.plan_id, r.action_index, r.action_type, r.status, r.t, r.detail]);
const TERMINAL = new Set(["SUCCESS", "SUCCEEDED", "FAILED", "FAILURE", "PREEMPTED", "ABORTED", "CANCELED", "CANCELLED"]);

/** A finished manipulation step (the executor names actions after the command class). */
export function isGraspResult(r: ActionResult): boolean {
  return /^(PICK|DRAG)$/i.test(r.action_type ?? "") && TERMINAL.has((r.status ?? "").toUpperCase());
}

interface GraspCamState {
  enabled: boolean;
  session: GraspSession | null;
  /** how the last session ended (shown briefly / for tests) */
  last: {robot: string; reason: GraspEndReason; at: number} | null;
  setEnabled: (on: boolean) => void;
  /** the operator answered request `requestId`; `cams`: the robot's camera list (null: unknown) */
  answered: (a: {robot: string; requestId: string; kind: GraspAnswer; cams: string[] | null}, now?: number) => void;
  /** a pending manipulation request is visible */
  requestSeen: (robot: string, requestId: string) => void;
  /** a robot's plan view changed (action results, lease) */
  planUpdate: (robot: string, plan: RobotPlan | undefined) => void;
  tick: (now?: number) => void;
  end: (reason: GraspEndReason, now?: number) => void;
}

export const useGraspCamera = create<GraspCamState>((set, get) => ({
  enabled: loadGraspEnabled(),
  session: null,
  last: null,
  setEnabled: on => {
    saveGraspEnabled(on);
    set({enabled: on});
    if (!on && get().session) get().end("disabled");
  },
  answered: ({robot, requestId, kind, cams}, now = Date.now()) => {
    const cur = get().session;
    if (kind !== "approve") {
      if (cur?.robot === robot) get().end(kind === "walk" ? "walk_to" : "reject", now);
      return;
    }
    if (!get().enabled || !cams?.includes(GRASP_CAM)) {
      if (cur?.robot === robot) get().end("new_request", now);
      return;
    }
    if (cur && cur.robot !== robot) get().end("new_request", now);
    const plan = useRobots.getState().plan[robot];
    // focus the robot so its camera view is the one shown; put the old focus back after
    const v = useViewer.getState();
    const keepFocus = cur?.robot === robot ? cur.prevFocus : null;
    let prevFocus: string | null = keepFocus;
    // never pull a full-screen drive session onto another robot (it would disarm)
    if (v.focus !== robot && !v.driveFull) {
      prevFocus = v.focus;
      v.setFocus(robot);
    }
    set({
      session: {
        robot,
        requestId,
        startedAt: now,
        deadline: now + GRASP_TIMEOUT_MS,
        baseline: (plan?.results ?? []).map(resultKey),
        leaseOwned: plan?.lease?.owned === true,
        prevFocus,
      },
    });
  },
  requestSeen: (robot, requestId) => {
    const s = get().session;
    if (s && s.robot === robot && s.requestId !== requestId) get().end("new_request");
  },
  planUpdate: (robot, plan) => {
    const s = get().session;
    if (!s || s.robot !== robot || !plan) return;
    const seen = new Set(s.baseline);
    if ((plan.results ?? []).some(r => !seen.has(resultKey(r)) && isGraspResult(r))) return get().end("result");
    const owned = plan.lease?.known !== false ? plan.lease?.owned : undefined;
    if (owned === true && !s.leaseOwned) set({session: {...s, leaseOwned: true}});
    else if (owned === false && s.leaseOwned) get().end("lease");
  },
  tick: (now = Date.now()) => {
    const s = get().session;
    if (s && now >= s.deadline) get().end("timeout", now);
  },
  end: (reason, now = Date.now()) => {
    const s = get().session;
    if (!s) return;
    set({session: null, last: {robot: s.robot, reason, at: now}});
    // the operator's own focus change wins; only undo ours
    const v = useViewer.getState();
    if (s.prevFocus !== null && reason !== "manual" && v.focus === s.robot) v.setFocus(s.prevFocus as Parameters<typeof v.setFocus>[0]);
  },
}));

/** Feed the session from the robots store and a 1 s timeout tick; returns an unbind. */
export function bindGraspCamera(): () => void {
  const unsub = useRobots.subscribe((st, prev) => {
    const s = useGraspCamera.getState().session;
    if (s && st.plan[s.robot] !== prev.plan[s.robot]) useGraspCamera.getState().planUpdate(s.robot, st.plan[s.robot]);
  });
  const id = window.setInterval(() => useGraspCamera.getState().tick(), 1000);
  return () => {
    unsub();
    window.clearInterval(id);
  };
}

/** The grasp session showing `robot`'s hand camera, if any (cams: the view's camera list). */
export function useGraspOverride(robot: string, cams: readonly string[] | null): GraspSession | null {
  const s = useGraspCamera(st => (st.session?.robot === robot ? st.session : null));
  return s && cams?.includes(GRASP_CAM) ? s : null;
}
