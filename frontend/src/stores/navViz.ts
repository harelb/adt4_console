// On-demand navigation overlays: per-robot "local planner" toggle (off by
// default) for the Husky's MPPI / mid-range planner markers. Turning it on asks
// the console to subscribe (POST /api/live/<r>/nav_viz) and renews that lease
// while it stays on; the data arrive on `robot/<r>/nav_viz`.
import {useEffect} from "react";
import {create} from "zustand";
import {errorText, postJSON} from "../lib/api";
import {parseNavViz, type NavVizPayload} from "../lib/navViz";
import {ws} from "../ws";

export type NavGroup = "planner";

/** the server drops a lease after 30 s; renew well before */
export const NAV_LEASE_RENEW_MS = 10_000;

interface NavVizStore {
  plannerOn: Record<string, boolean>;
  planner: Record<string, NavVizPayload>;
  /** robot -> last error (null = fine) */
  error: Record<string, string | null>;
  /** robot -> topics the planner group subscribes (empty: nothing for that platform) */
  topics: Record<string, string[]>;
  set: (patch: Partial<NavVizStore>) => void;
}

export const useNavViz = create<NavVizStore>(set => ({
  plannerOn: {},
  planner: {},
  error: {},
  topics: {},
  set: patch => set(patch as Partial<NavVizStore>),
}));

/** Ask the console to start / renew / stop the planner overlay for ``robot``. */
export async function requestNavViz(robot: string, group: NavGroup, enabled: boolean): Promise<void> {
  try {
    const r = await postJSON<{topics?: string[]}>(`api/live/${encodeURIComponent(robot)}/nav_viz`, {group, enabled});
    useNavViz.setState(s => ({error: {...s.error, [robot]: null}, ...(r?.topics ? {topics: {...s.topics, [robot]: r.topics}} : {})}));
  } catch (e) {
    useNavViz.setState(s => ({error: {...s.error, [robot]: errorText(e)}}));
  }
}

/** Turn the planner overlay on / off for ``robot`` (the lease hook follows). */
export function toggleNavViz(robot: string, on: boolean) {
  useNavViz.setState(s => ({plannerOn: {...s.plannerOn, [robot]: on}}));
  if (!on) {
    void requestNavViz(robot, "planner", false);
    useNavViz.setState(s => {
      const planner = {...s.planner};
      delete planner[robot];
      return {planner};
    });
  }
}

/** While mounted with ``on``: start the lease now and renew it every NAV_LEASE_RENEW_MS. */
export function useNavVizLease(robot: string, group: NavGroup, on: boolean) {
  useEffect(() => {
    if (!on) return;
    void requestNavViz(robot, group, true);
    const h = window.setInterval(() => void requestNavViz(robot, group, true), NAV_LEASE_RENEW_MS);
    return () => window.clearInterval(h);
  }, [robot, group, on]);
}

/** Route `robot/<r>/nav_viz` frames into the store (only while that robot's
 *  toggle is on). Returns the unsubscribe. */
export function bindNavViz(): () => void {
  return ws.on("robot/", (d, f) => {
    const m = /^robot\/([^/]+)\/nav_viz$/.exec(f.topic);
    if (!m) return;
    const r = m[1];
    const p = parseNavViz(d);
    const on = !!useNavViz.getState().plannerOn[r];
    useNavViz.setState(s => {
      const planner = {...s.planner};
      if (p && on) planner[r] = p;
      else delete planner[r];
      return {planner};
    });
  });
}
