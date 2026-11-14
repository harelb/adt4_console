// On-demand navigation overlays: per-robot toggles (all off by default) for the
// planner markers (Husky MPPI / mid-range) and the GPS fix. Turning one on asks
// the console to subscribe (POST /api/live/<r>/nav_viz) and renews that lease
// while it stays on; the data arrive on `robot/<r>/nav_viz` / `robot/<r>/gps`.
import {useEffect} from "react";
import {create} from "zustand";
import {errorText, postJSON} from "../lib/api";
import {parseGps, parseNavViz, type GpsFix, type NavVizPayload} from "../lib/navViz";
import {ws} from "../ws";

export type NavGroup = "planner" | "gps";

/** the server drops a lease after 30 s; renew well before */
export const NAV_LEASE_RENEW_MS = 10_000;

interface NavVizStore {
  plannerOn: Record<string, boolean>;
  gpsOn: Record<string, boolean>;
  /** draw a line from the GPS fix to the relocalized pose */
  gpsLine: boolean;
  planner: Record<string, NavVizPayload>;
  gps: Record<string, GpsFix>;
  /** robot -> group -> last error (null = fine) */
  error: Record<string, Partial<Record<NavGroup, string | null>>>;
  /** robot -> topics the planner group subscribes (empty: nothing for that platform) */
  topics: Record<string, string[]>;
  set: (patch: Partial<NavVizStore>) => void;
}

export const useNavViz = create<NavVizStore>(set => ({
  plannerOn: {},
  gpsOn: {},
  gpsLine: true,
  planner: {},
  gps: {},
  error: {},
  topics: {},
  set: patch => set(patch as Partial<NavVizStore>),
}));

const onKey = (g: NavGroup) => (g === "planner" ? "plannerOn" : "gpsOn");

function setError(robot: string, group: NavGroup, msg: string | null) {
  useNavViz.setState(s => ({error: {...s.error, [robot]: {...s.error[robot], [group]: msg}}}));
}

/** Ask the console to start / renew / stop ``group`` for ``robot``. */
export async function requestNavViz(robot: string, group: NavGroup, enabled: boolean): Promise<void> {
  try {
    const r = await postJSON<{topics?: string[]}>(`api/live/${encodeURIComponent(robot)}/nav_viz`, {group, enabled});
    setError(robot, group, null);
    if (group === "planner" && r?.topics) useNavViz.setState(s => ({topics: {...s.topics, [robot]: r.topics!}}));
  } catch (e) {
    setError(robot, group, errorText(e));
  }
}

/** Turn ``group`` on / off for ``robot`` (remembers it; the lease hook follows). */
export function toggleNavViz(robot: string, group: NavGroup, on: boolean) {
  const k = onKey(group);
  useNavViz.setState(s => ({[k]: {...s[k], [robot]: on}}) as Partial<NavVizStore>);
  if (!on) {
    void requestNavViz(robot, group, false);
    useNavViz.setState(s => {
      const data = {...(group === "planner" ? s.planner : s.gps)};
      delete data[robot];
      return group === "planner" ? {planner: data as Record<string, NavVizPayload>} : {gps: data as Record<string, GpsFix>};
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

/** Route `robot/<r>/nav_viz` and `robot/<r>/gps` frames into the store
 *  (only while that robot's toggle is on). Returns the unsubscribe. */
export function bindNavViz(): () => void {
  return ws.on("robot/", (d, f) => {
    const m = /^robot\/([^/]+)\/(nav_viz|gps)$/.exec(f.topic);
    if (!m) return;
    const [, r, kind] = m;
    const st = useNavViz.getState();
    if (kind === "nav_viz") {
      const p = parseNavViz(d);
      useNavViz.setState(s => {
        const planner = {...s.planner};
        if (p && st.plannerOn[r]) planner[r] = p;
        else delete planner[r];
        return {planner};
      });
    } else {
      const g = parseGps(d);
      useNavViz.setState(s => {
        const gps = {...s.gps};
        if (g && st.gpsOn[r]) gps[r] = g;
        else delete gps[r];
        return {gps};
      });
    }
  });
}
