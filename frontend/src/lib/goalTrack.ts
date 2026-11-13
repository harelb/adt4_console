// Live "did the robot get it" status of a goal the console sent (backend
// goal_tracker): GET api/goal_tracks/<id>, then goal_track/<id> hub pushes.
import {create} from "zustand";
import {getJSON} from "./api";
import {ws} from "../ws";
import type {Tone} from "../components/ui";

export interface TrackEvent {
  t: number;
  text: string;
}

export interface RobotTrackView {
  robot: string;
  named: boolean;
  plan_id: string | null;
  actions: string[];
  n: number;
  done: number;
  delivered: number | null;
  received: number | null;
  received_how: string;
  paused: boolean;
  lease: "wait" | "unknown" | null;
  lease_owner: string;
  stage: string;
  text: string;
  events: TrackEvent[];
}

export interface GoalTrackView {
  id: string;
  t: number;
  robot: string;
  kind: string;
  via: string | null;
  summary: string;
  planner: string;
  planner_error: string;
  stage: string;
  text: string;
  robots: Record<string, RobotTrackView>;
  events: TrackEvent[];
}

interface TrackStore {
  tracks: Record<string, GoalTrackView | null>;
}

export const useGoalTracks = create<TrackStore>(() => ({tracks: {}}));

let listening = false;
function listen() {
  if (listening) return;
  listening = true;
  ws.on("goal_track/", (d, f) => {
    const id = f.topic.slice("goal_track/".length);
    useGoalTracks.setState(s => ({tracks: {...s.tracks, [id]: d as GoalTrackView}}));
  });
  ws.onReconnect(() => {
    for (const id of Object.keys(useGoalTracks.getState().tracks)) void fetchTrack(id);
  });
}

async function fetchTrack(id: string) {
  try {
    const v = await getJSON<GoalTrackView>(`api/goal_tracks/${encodeURIComponent(id)}`);
    useGoalTracks.setState(s => ({tracks: {...s.tracks, [id]: v}}));
  } catch {
    // unknown id (the console restarted): keep what we had, else mark it gone
    useGoalTracks.setState(s => (s.tracks[id] ? s : {tracks: {...s.tracks, [id]: null}}));
  }
}

/** Start following a tracked goal (idempotent). */
export function followTrack(id: string) {
  listen();
  if (!(id in useGoalTracks.getState().tracks)) {
    useGoalTracks.setState(s => ({tracks: {...s.tracks, [id]: s.tracks[id] ?? null}}));
    void fetchTrack(id);
  }
}

const TONE: Record<string, Tone> = {
  planning: "info",
  planned: "info",
  delivered: "info",
  waiting: "info",
  executing: "ok",
  done: "ok",
  paused: "warn",
  lease_wait: "warn",
  lease_unknown: "warn",
  stopped: "warn",
  preempted: "warn",
  no_plan: "off",
  plan_failed: "err",
  failed: "err",
};

export function stageTone(stage: string): Tone {
  return TONE[stage] ?? "off";
}

const LABEL: Record<string, string> = {
  planning: "planning",
  planned: "planned",
  delivered: "delivered",
  waiting: "waiting for plan",
  executing: "executing",
  done: "done",
  paused: "paused",
  lease_wait: "waiting: lease",
  lease_unknown: "lease unknown",
  stopped: "not executing",
  preempted: "preempted",
  no_plan: "no plan",
  plan_failed: "planning failed",
  failed: "failed",
};

export function stageLabel(stage: string): string {
  return LABEL[stage] ?? stage;
}

const ENDED = new Set(["done", "failed", "preempted"]);

function clock(t: number): string {
  const d = new Date(t * 1000);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/** One robot's step, e.g. "received · executing 2/3 (GAZE)"; an ended step
 *  says when ("done: all 2 actions succeeded · at 18:04:12"). */
export function robotLine(r: RobotTrackView): string {
  const got = r.received != null ? "received" : r.delivered != null ? "delivered" : r.plan_id ? "not yet received" : "";
  const last = r.events.length ? r.events[r.events.length - 1].t : null;
  const when = ENDED.has(r.stage) && last != null ? `at ${clock(last)}` : "";
  return [got && r.stage !== "done" && r.stage !== "no_plan" ? got : "", r.text, when].filter(Boolean).join(" · ");
}

/** Stages worth telling the chat model about (once each). */
export const NOTABLE = new Set(["plan_failed", "done", "failed", "preempted", "paused", "lease_wait", "stopped"]);

/** "[operator] status of goal ...": a line for the chat transcript. */
export function trackNote(v: GoalTrackView): string {
  return `[operator] status of the goal sent to ${v.robot}${v.summary ? ` (${v.summary})` : ""}: ${stageLabel(v.stage)} — ${v.text}`;
}
