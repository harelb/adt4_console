// Trials: one recording across the fleet (API.md §23). Types of the
// `trial/state` payload and what the recording bar shows for it.
import type {Tone} from "../components/ui";

export type MachineState =
  | "starting" | "recording" | "failed" | "unreachable" | "lost"
  | "stopping" | "stopped" | "stop_pending" | "stop_failed" | "skipped";

export interface TrialMachine {
  name: string;
  role?: string | null;
  state: MachineState;
  message?: string | null;
  /** taken out of the trial while it went on (its bag is closed) */
  removed?: boolean;
  dir?: string | null;
  bytes?: number | null;
  rate_bps?: number | null;
  free_bytes?: number | null;
  minutes_left?: number | null;
  /** console clock (unix s) of the machine's last answer */
  last_seen?: number | null;
  warnings?: string[];
  clock_offset_s?: number | null;
  topic_check?: {ok: boolean; tries?: number; retrying?: boolean; published?: number; topics?: number; missing?: string[]; error?: string;
    /** console clock (unix s) of the answer shown, and of the last try; the check is repeated while recording */
    checked?: number; tried?: number; recheck_error?: string | null} | null;
}

export interface Trial {
  trial_id: string;
  description: string;
  state: "starting" | "recording" | "stopping" | "stopped" | "failed";
  started: number;
  stopped: number | null;
  events: number;
  flags: number;
  log_error?: string | null;
  note?: string | null;
  console_dir?: string;
  machines: Record<string, TrialMachine>;
}

export interface TrialPayload {
  now: number;
  poll_s: number;
  active: Trial | null;
  last: Trial | null;
}

export interface PreflightMachine {
  name: string;
  role?: string | null;
  ready: boolean;
  /** a recorder is already running there (the trial or bag it records), from a trial this console does not have */
  recording?: string | boolean | null;
  /** ... and a start may stop it and record anew */
  replaceable?: boolean;
  /** this console's own machine: the base station */
  local?: boolean;
  error: string | null;
  free_bytes: number | null;
  total_bytes: number | null;
  rate_bps: number | null;
  minutes: number | null;
  notes: string[];
}

/** A machine that is, or may be, still recording. */
export const LIVE: MachineState[] = ["recording", "unreachable", "stop_pending", "stop_failed", "stopping"];

/** What the chip says about the topic check (which topics have a publisher). */
export function topicCheckLabel(m: TrialMachine): {text: string; bad: boolean} {
  const tc = m.topic_check;
  if (!tc) return {text: "topic check pending", bad: false};
  if (!tc.ok) return tc.retrying ? {text: `topic check got no answer, retrying (${tc.tries ?? 1})`, bad: true} : {text: "TOPIC CHECK DID NOT RUN", bad: true};
  const n = tc.missing?.length ?? 0;
  if (n) return {text: `${n}/${tc.topics} topics unpublished`, bad: true};
  return {text: `${tc.published}/${tc.topics} topics`, bad: false};
}

/** Seconds since the machine last answered, on the console server's clock. */
export function answerAge(m: TrialMachine, serverNow: number): number | null {
  return m.last_seen ? Math.max(0, serverNow - m.last_seen) : null;
}

/** An answer older than this is stale: 2.5 polls, at least 15 s. */
export const staleAfter = (pollS: number) => Math.max(15, 2.5 * pollS);

/** Green only for a recording that answered recently and has no warning:
 *  anything unknown is amber, anything known to be wrong is red. */
export function machineTone(m: TrialMachine, serverNow: number, pollS: number): Tone {
  switch (m.state) {
    case "failed":
    case "lost":
    case "stop_failed":
      return "err";
    case "unreachable":
    case "stop_pending":
    case "starting":
    case "stopping":
      return "warn";
    case "skipped":
      return "off";
    case "stopped":
      if (m.removed) return "off";
      return m.warnings?.length ? "warn" : "ok";
    case "recording": {
      const age = answerAge(m, serverNow);
      if (age === null || age > staleAfter(pollS)) return "warn";
      if (m.warnings?.length) return "warn";
      // not green until the topic check has run and found every topic published
      if (!m.topic_check || !m.topic_check.ok || m.topic_check.missing?.length) return "warn";
      return "ok";
    }
  }
}

export function machineLabel(m: TrialMachine): string {
  switch (m.state) {
    case "recording":
      return "recording";
    case "unreachable":
      return "no answer";
    case "lost":
      return "NOT recording";
    case "failed":
      return "did not start";
    case "stop_pending":
      return "stop not delivered";
    case "stop_failed":
      return "would not stop";
    case "stopped":
      return m.removed ? "removed" : "stopped";
    default:
      return m.state;
  }
}

export function fmtElapsed(s: number): string {
  s = Math.max(0, Math.floor(s));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const pad = (n: number) => String(n).padStart(2, "0");
  return h ? `${h}:${pad(m)}:${pad(sec)}` : `${pad(m)}:${pad(sec)}`;
}

export function fmtMinutes(min: number | null | undefined): string {
  if (min === null || min === undefined || !Number.isFinite(min)) return "?";
  if (min >= 48 * 60) return "days";
  if (min >= 120) return `${(min / 60).toFixed(1)} h`;
  return `${Math.round(min)} min`;
}

export function fmtAge(s: number | null): string {
  if (s === null) return "never";
  if (s < 90) return `${Math.round(s)} s ago`;
  return `${Math.round(s / 60)} min ago`;
}

/** The shortest time any recording machine has left, in minutes. */
export function shortestLeft(t: Trial): {name: string; minutes: number} | null {
  let best: {name: string; minutes: number} | null = null;
  for (const m of Object.values(t.machines)) {
    if (m.state !== "recording" || m.minutes_left === null || m.minutes_left === undefined) continue;
    if (!best || m.minutes_left < best.minutes) best = {name: m.name, minutes: m.minutes_left};
  }
  return best;
}

/** Taken out of the trial on purpose, and its bag is closed. */
export const isRemoved = (m: TrialMachine) => !!m.removed && m.state === "stopped";

/** How many of the trial's machines are known to be recording right now
 *  (a machine that was removed is not one of them). */
export function recordingCount(t: Trial, serverNow: number, pollS: number): {ok: number; of: number} {
  const ms = Object.values(t.machines).filter(m => m.state !== "skipped" && !isRemoved(m));
  return {ok: ms.filter(m => m.state === "recording" && machineTone(m, serverNow, pollS) !== "err" && (answerAge(m, serverNow) ?? Infinity) <= staleAfter(pollS)).length, of: ms.length};
}
