// "A robot's part of a goal ended" (backend goal_tracker -> hub ``goal_event``):
// a toast on every page and one line in the chat log, so the operator sees a
// robot reach (or fail) its goal without watching the goal card.
import {create} from "zustand";
import {ws} from "../ws";
import {useChat} from "../stores/chat";

export interface GoalEvent {
  goal: string;
  robot: string;
  /** done / failed / preempted */
  stage: string;
  text: string;
  summary: string;
  kind: string;
  /** console clock (s) */
  t: number;
}

export interface GoalEventToast extends GoalEvent {
  key: string;
}

/** How long a toast stays (ms); a failure stays until dismissed. */
export const TOAST_MS = 10000;
const MAX_TOASTS = 4;

interface Store {
  toasts: GoalEventToast[];
}

export const useGoalEvents = create<Store>(() => ({toasts: []}));

export function eventKey(e: GoalEvent): string {
  return `${e.goal}/${e.robot}/${e.stage}`;
}

function clock(t: number): string {
  const d = new Date(t * 1000);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/** "hamilton reached “go to the tree” at 18:04:12". */
export function eventLine(e: GoalEvent): string {
  const what = e.summary ? `“${e.summary}”` : `its ${e.kind || ""} goal`.replace("  ", " ");
  const verb = e.stage === "done" ? "reached" : e.stage === "failed" ? "failed" : "stopped";
  return `${e.robot} ${verb} ${what} at ${clock(e.t)}`;
}

export function dismissGoalEvent(key: string): void {
  useGoalEvents.setState(s => ({toasts: s.toasts.filter(t => t.key !== key)}));
}

/** Handle one event: a toast (deduplicated) and a chat line (once per goal,
 *  robot and stage, also across reloads since the chat is persisted). */
export function onGoalEvent(e: GoalEvent): void {
  const key = eventKey(e);
  if (!useGoalEvents.getState().toasts.some(t => t.key === key)) {
    useGoalEvents.setState(s => ({toasts: [...s.toasts, {...e, key}].slice(-MAX_TOASTS)}));
    if (e.stage !== "failed") window.setTimeout(() => dismissGoalEvent(key), TOAST_MS);
  }
  const items = useChat.getState().items;
  if (!items.some(it => it.type === "goal_event" && eventKey(it.event) === key)) {
    useChat.setState({items: [...items, {type: "goal_event", event: e}]});
  }
}

let listening = false;
/** Start listening for goal events (idempotent). */
export function listenGoalEvents(): void {
  if (listening) return;
  listening = true;
  ws.on("goal_event", d => onGoalEvent(d as GoalEvent));
}
