// Penn's quad in the console (API.md §16): its state for the map, goals the
// chat proposes, and what happens when one arrives (backend ``quad_event``):
// a toast and a chat line; a capture shows up in the chat; and the browser tab
// that sent the goal starts a follow-up chat turn with the capture attached,
// so the assistant looks at the picture.
import {create} from "zustand";
import {ws} from "../ws";
import {postJSON} from "./api";
import {onGoalEvent} from "./goalEvents";
import {useChat} from "../stores/chat";

export interface QuadPose {
  x: number;
  y: number;
  z?: number | null;
  yaw?: number | null;
  t?: number | null;
}

export interface QuadGoal {
  id: string;
  x: number;
  y: number;
  capture: boolean;
  query: string;
  label: string;
  status: "flying" | "arrived" | "failed" | "preempted";
  capture_id?: string | null;
  error?: string | null;
  chat?: {client?: string; follow_up?: boolean} | null;
}

export interface QuadCapture {
  id: string;
  image_url: string;
  overlay_url?: string | null;
  pose_map?: QuadPose | null;
  segment?: {query: string; detections?: {label: string; score: number; map_xy_approx?: number[]}[]; error?: string} | null;
}

export interface QuadState {
  configured: boolean;
  pose?: QuadPose | null;
  goal?: QuadGoal | null;
  error?: string | null;
}

/** A follow-up chat turn waiting for the chat panel (it runs it when idle). */
export interface QuadFollowUp {
  message: string;
  attach: {quad_capture: string};
}

interface Store {
  state: QuadState | null;
  followUps: QuadFollowUp[];
}

export const useQuad = create<Store>(() => ({state: null, followUps: []}));

/** This browser tab: only the tab that sent a goal runs its follow-up turn. */
export const CLIENT_ID = (() => {
  try {
    const k = "adt4-quad-client";
    const v = sessionStorage.getItem(k) ?? Math.random().toString(36).slice(2, 10);
    sessionStorage.setItem(k, v);
    return v;
  } catch {
    return Math.random().toString(36).slice(2, 10);
  }
})();

export interface QuadGoalCard {
  x: number;
  y: number;
  label: string;
  capture: boolean;
  query: string;
}

/** Send an approved quad goal; the follow-up on arrival belongs to this tab. */
export async function sendQuadGoal(c: QuadGoalCard, followUp = true): Promise<QuadGoal> {
  const out = await postJSON<{goal: QuadGoal}>("api/quad/goal", {
    x: c.x,
    y: c.y,
    capture: c.capture,
    query: c.query,
    label: c.label,
    chat: {client: CLIENT_ID, follow_up: followUp},
  });
  return out.goal;
}

const where = (g: QuadGoal) => g.label || `(${g.x.toFixed(1)}, ${g.y.toFixed(1)})`;

/** The message of the follow-up turn the assistant gets with the picture. */
export function followUpMessage(g: QuadGoal, c: QuadCapture): string {
  const seg = c.segment;
  const found = seg?.query ? (seg.error ? ` SAM3 on '${seg.query}' failed: ${seg.error}.` : ` SAM3 looked for '${seg.query}' (overlay attached).`) : "";
  return `[quad] arrived over ${where(g)} and took capture ${c.id}.${found} Describe what you see and answer my request from it.`;
}

/** The chat card of a capture (same shape as show_object_images). */
export function captureImagesItem(c: QuadCapture) {
  const seg = c.segment;
  let label = `quad capture ${c.id}`;
  if (seg?.query) label += seg.error ? ` · SAM3 failed` : ` · SAM3 '${seg.query}': ${seg.detections?.length ?? 0} found`;
  const p = c.pose_map;
  return {
    type: "images" as const,
    src: "quad",
    items: [{sym: c.id, label, pos: p ? [p.x, p.y] : null, images: [c.overlay_url, c.image_url].filter((u): u is string => !!u)}],
  };
}

const shown = new Set<string>();

/** One ``quad_event``: a toast + chat line, the capture in the chat, the follow-up. */
export function onQuadEvent(ev: {kind: string; t: number; goal?: QuadGoal; capture?: QuadCapture; error?: string}): void {
  const g = ev.goal;
  if (!g) return;
  const stage = ev.kind === "arrived" ? "done" : ev.kind === "failed" || ev.kind === "capture_failed" ? "failed" : ev.kind === "preempted" ? "preempted" : null;
  if (stage)
    onGoalEvent({
      goal: `quad-${g.id}-${ev.kind}`,
      robot: "penn quad",
      stage,
      text: ev.error ?? g.error ?? "",
      summary: `fly over ${where(g)}`,
      kind: "quad",
      t: ev.t,
    });
  if (ev.kind !== "captured" || !ev.capture || shown.has(ev.capture.id)) return;
  shown.add(ev.capture.id);
  const items = useChat.getState().items;
  if (!items.some(it => it.type === "images" && it.src === "quad" && it.items[0]?.sym === ev.capture!.id))
    useChat.setState({items: [...items, captureImagesItem(ev.capture)]});
  if (g.chat?.client === CLIENT_ID && g.chat.follow_up)
    useQuad.setState(s => ({followUps: [...s.followUps, {message: followUpMessage(g, ev.capture!), attach: {quad_capture: ev.capture!.id}}]}));
}

/** Take the next follow-up turn off the queue (the chat panel runs it). */
export function takeFollowUp(): QuadFollowUp | null {
  const q = useQuad.getState().followUps;
  if (!q.length) return null;
  useQuad.setState({followUps: q.slice(1)});
  return q[0];
}

let listening = false;
/** Start listening for the quad's state and events (idempotent). */
export function listenQuad(): void {
  if (listening) return;
  listening = true;
  ws.on("quad/state", d => useQuad.setState({state: d as QuadState}));
  ws.on("quad_event", d => onQuadEvent(d as Parameters<typeof onQuadEvent>[0]));
}
