// The Launch form's last choices (GET api/launch/defaults → `last`, saved server-side
// per base station on every launch: routes/launch.py, launch_state.LaunchMemory).

import type {LaunchMode} from "./launchLink";

export interface LastLaunch {
  robots?: string[] | null;
  session?: string | null;
  experiment?: string | null;
  mode?: string | null;
  prior_path?: string | null;
  prior_source?: string | null;
  record?: boolean | null;
  base_station?: string | null;
  bs_session?: string | null;
  link_network?: string | null;
  link_mode?: string | null;
  saved_at?: number | null;
}

/** The form's own timestamped name: `map_20261111_1402` / `reloc_...`. */
export function defaultExperiment(mode: LaunchMode, d = new Date()): string {
  const p = (n: number) => String(n).padStart(2, "0");
  return `${mode === "mapping" ? "map" : "reloc"}_${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}_${p(d.getHours())}${p(d.getMinutes())}`;
}

const AUTO_NAME = /^(map|reloc)_\d{8}_\d{4}$/;

/** Name for a relaunch of `last`: a fresh timestamp when it was the form's own
 * default name, else the same name with its `_vN` suffix bumped (`exp` -> `exp_v2`,
 * `exp_v2` -> `exp_v3`) so the last run's output dir is kept. */
export function nextExperimentName(last: string | null | undefined, mode: LaunchMode, now = new Date()): string {
  const name = (last ?? "").trim();
  if (!name || AUTO_NAME.test(name)) return defaultExperiment(mode, now);
  const m = /^(.*)_v(\d+)$/.exec(name);
  return m ? `${m[1]}_v${Number(m[2]) + 1}` : `${name}_v2`;
}

export const isLaunchMode = (m: unknown): m is LaunchMode => m === "mapping" || m === "relocalize";

/** "machine\u0000path" prior value of the Launch form, or "" for none. */
export function lastPriorValue(last: LastLaunch | null | undefined, base: string): string {
  if (!last?.prior_path) return "";
  return `${last.prior_source || base}\u0000${last.prior_path}`;
}
