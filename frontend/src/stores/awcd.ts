// The AWCD change set (lib/awcd.ts) and its display switches. One feed per
// page: useAwcdFeed() is mounted by the 3D overlay (always in the viewer) and
// the change list; it loads the set once and follows hub topic awcd/changes.
import {useEffect} from "react";
import {create} from "zustand";
import {getJSON} from "../lib/api";
import type {AwcdState} from "../lib/awcd";
import {ws} from "../ws";

export const AWCD_TOPIC = "awcd/changes";

interface AwcdStore {
  state: AwcdState | null;
  /** draw the boxes on the map (default on) */
  show: boolean;
  labels: boolean;
  /** robots whose reconciled changes are hidden on the map ("?" = unattributed) */
  hiddenRobots: Record<string, boolean>;
  /** draw each robot's raw report (/awcd_changes, its own frame) too (default off) */
  showRaw: boolean;
  /** the change picked in the list (highlighted on the map) */
  focus: string | null;
  set: (patch: Partial<AwcdStore>) => void;
}

function readBool(key: string, dflt: boolean): boolean {
  try {
    const v = localStorage.getItem(key);
    return v === null ? dflt : v === "1";
  } catch {
    return dflt;
  }
}

function readHidden(): Record<string, boolean> {
  try {
    const v = JSON.parse(localStorage.getItem("adt4.awcd.hiddenRobots") ?? "{}");
    return v && typeof v === "object" ? v : {};
  } catch {
    return {};
  }
}

export function setRobotHidden(robot: string, hidden: boolean) {
  const next = {...useAwcd.getState().hiddenRobots, [robot]: hidden};
  if (!hidden) delete next[robot];
  try {
    localStorage.setItem("adt4.awcd.hiddenRobots", JSON.stringify(next));
  } catch {
    /* private window */
  }
  useAwcd.setState({hiddenRobots: next});
}

export function saveBool(key: string, v: boolean) {
  try {
    localStorage.setItem(key, v ? "1" : "0");
  } catch {
    /* private window */
  }
}

export const useAwcd = create<AwcdStore>(set => ({
  state: null,
  show: readBool("adt4.awcd.show", true),
  labels: readBool("adt4.awcd.labels", true),
  hiddenRobots: readHidden(),
  showRaw: readBool("adt4.awcd.raw", false),
  focus: null,
  set: patch => set(patch as Partial<AwcdStore>),
}));

let users = 0;
let off: (() => void) | null = null;

function startFeed() {
  getJSON<AwcdState>("api/change_detection/changes").then(
    d => useAwcd.setState({state: d}),
    () => undefined,
  );
  const unsub = ws.on(AWCD_TOPIC, d => {
    if (d && typeof d === "object") useAwcd.setState({state: d as AwcdState});
  });
  return unsub;
}

/** Keep the change set current while any user is mounted. */
export function useAwcdFeed() {
  useEffect(() => {
    if (users++ === 0) off = startFeed();
    return () => {
      if (--users === 0) {
        off?.();
        off = null;
      }
    };
  }, []);
}
