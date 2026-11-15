// Mission logging: the session, each robot's relocalization state, the gate and
// the open mission (backend logbook.py; GET api/logbook + hub "logbook/state").
import {create} from "zustand";
import {getJSON, postJSON} from "../lib/api";
import {ws} from "../ws";

export type RelocState = "unlocalized" | "relocalizing" | "relocalized" | "failed" | "overridden" | "not_required";

export interface RelocEvidence {
  verdict?: string | null;
  ok?: boolean;
  reason?: string | null;
  hydra_accepted?: number | null;
  hydra_total?: number | null;
  roman_prior_lcs?: number | null;
  map_T_odom?: number[] | null;
  live?: boolean;
}

export interface RobotReloc {
  state: RelocState;
  t: number;
  operator?: string | null;
  note?: string | null;
  reason?: string | null;
  mode?: string | null;
  t_started?: number | null;
  degraded?: {t: number; jump_m: number; jump_deg: number} | null;
  evidence?: RelocEvidence;
  history?: {state: string; t: number; operator?: string | null; reason?: string | null; note?: string | null}[];
}

export interface Mission {
  mission_id: string;
  name?: string | null;
  state: "open" | "ended";
  session_id?: string | null;
  t_start: number;
  t_end?: number | null;
  start_reason: string;
  end_reason?: string | null;
  goals: string[];
  robots: string[];
}

export interface Session {
  session_id: string;
  state: "open" | "stopped";
  t_start: number;
  experiment?: string | null;
  mode?: string | null;
  robots: string[];
  reloc_required?: boolean;
}

export interface LogbookState {
  session: Session | null;
  reloc: Record<string, RobotReloc>;
  gate: {ok: boolean; reasons: string[]; blocking: string[]; session?: boolean};
  mission: Mission | null;
  recent?: Mission[];
  base_dir?: string;
  persist?: boolean;
  t?: number;
}

interface Store {
  state: LogbookState | null;
  error: string | null;
  set: (s: LogbookState) => void;
}

export const useLogbook = create<Store>(set => ({
  state: null,
  error: null,
  set: s =>
    set(cur => {
      // hub pushes carry no live evidence: keep the last fetched one per robot
      const prev = cur.state?.reloc ?? {};
      const reloc: Record<string, RobotReloc> = {};
      for (const [r, v] of Object.entries(s.reloc ?? {})) reloc[r] = {...v, evidence: v.evidence ?? prev[r]?.evidence};
      return {state: {...s, reloc}, error: null};
    }),
}));

/** A logbook view (an older backend / another route's answer is not one). */
export const isLogbookState = (d: unknown): d is LogbookState =>
  Boolean(d && typeof d === "object" && (d as LogbookState).gate && typeof (d as LogbookState).gate === "object");

export async function refreshLogbook(): Promise<void> {
  try {
    const d = await getJSON<unknown>("api/logbook");
    if (isLogbookState(d)) useLogbook.getState().set(d);
  } catch (e) {
    useLogbook.setState({error: e instanceof Error ? e.message : String(e)});
  }
}

let wired = 0;
let off: (() => void) | null = null;
let timer: number | null = null;
/** Follow the logbook while a component is mounted (hub pushes + a 5 s evidence poll). */
export function wireLogbook(): () => void {
  if (wired++ === 0) {
    void refreshLogbook();
    off = ws.on("logbook/state", d => {
      if (isLogbookState(d)) useLogbook.getState().set(d);
    });
    timer = window.setInterval(() => void refreshLogbook(), 5000);
  }
  return () => {
    if (--wired === 0) {
      off?.();
      off = null;
      if (timer !== null) window.clearInterval(timer);
      timer = null;
    }
  };
}

export async function logbookPost(path: string, body: unknown = {}): Promise<void> {
  const r = await postJSON<LogbookState>(`api/logbook/${path}`, body);
  if (isLogbookState(r)) useLogbook.getState().set(r);
  void refreshLogbook();
}

/** Why "Start mission" is disabled ("" = it is not). */
export function startBlockedReason(s: LogbookState | null): string {
  if (!s) return "mission logging state not loaded";
  if (s.mission) return `mission ${s.mission.name || s.mission.mission_id} is open`;
  if (!s.gate.ok) return `relocalize first: ${s.gate.reasons.join("; ")}`;
  return "";
}
