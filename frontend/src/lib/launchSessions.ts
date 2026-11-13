// Launch form: one robot session per platform (Spot vs Husky/phoenix) and the
// base-station-only mode.  The backend maps each robot to its platform
// (GET api/launch/defaults → robot_platforms; fleet platform_of: topology
// platform_type, missing → spot, husky → phoenix).

export const PLATFORM_LABEL: Record<string, string> = {spot: "Spot", phoenix: "Husky"};

export const platformLabel = (p: string) => PLATFORM_LABEL[p] ?? p;

/** A robot's platform: the backend's mapping, else the topology platform_type by
 * the same rule (missing → spot, husky → phoenix). */
export function platformOf(name: string, robotPlatforms: Record<string, string> | undefined, platformType?: string): string {
  const p = robotPlatforms?.[name];
  if (p) return p;
  const t = (platformType || "spot").trim().toLowerCase();
  return t === "husky" ? "phoenix" : t;
}

/** Platforms of the selected robots, in robot order, without repeats. */
export function platformsOf(robots: string[], platform: (r: string) => string): string[] {
  const out: string[] = [];
  for (const r of robots) {
    const p = platform(r);
    if (!out.includes(p)) out.push(p);
  }
  return out;
}

/** Session files for `platform`: spot* (spot-, spot_relocalize-, ...) or phoenix*. */
export function sessionsFor(all: string[], platform: string): string[] {
  const own = all.filter(s => s.startsWith(platform));
  return own.length ? own : all;
}

/** The platform a robot session is made for, by its experiment name
 * (`<experiment>-<config>`): spot* -> spot, phoenix* -> phoenix; undefined for
 * any other session (base station, hand-made). Same rule as the backend
 * (launch_workflow.session_platform). */
export function sessionPlatform(s: string | null | undefined): string | undefined {
  const exp = (s ?? "").split("-")[0];
  return ["spot", "phoenix"].find(p => exp.startsWith(p));
}

/** `s` fits a robot of `platform`: made for it, or for no platform. */
export const sessionFitsPlatform = (s: string | null | undefined, platform: string): boolean => {
  const own = sessionPlatform(s);
  return !own || own === platform;
};

/** `s` for a robot of `platform`: itself when it fits, else the other platform's
 * same session (spot_relocalize-X <-> phoenix_relocalize-X) when it exists in
 * `all`; undefined without one. */
export function platformEquivalent(s: string | null | undefined, platform: string, all: string[] | undefined): string | undefined {
  if (!s) return undefined;
  const own = sessionPlatform(s);
  if (!own || own === platform) return s;
  const swapped = platform + s.slice(own.length);
  return all?.includes(swapped) ? swapped : undefined;
}

/** The session a platform's picker shows: the user's pick for that platform, else
 * the single-session pick mapped to the platform (a phoenix robot never gets a
 * spot_* session: 2026-11-11), else the mode's preset. */
export function platformSession(platform: string, picked: Record<string, string>, session: string, presets: Record<string, string> | undefined, all?: string[]): string {
  if (picked[platform] && sessionFitsPlatform(picked[platform], platform)) return picked[platform];
  if (session && session.startsWith(platform)) return session;
  const mapped = sessionPlatform(session) ? platformEquivalent(session, platform, all) : undefined;
  if (mapped) return mapped;
  return presets?.[platform] ?? "";
}

/** Per-platform picks whose session is made for that platform. */
export function picksForPlatforms(picks: Record<string, string> | null | undefined): Record<string, string> {
  return Object.fromEntries(Object.entries(picks ?? {}).filter(([p, s]) => s && sessionFitsPlatform(s, p)));
}

/** session -> "mapping" | "relocalize" (GET api/launch/defaults → session_modes,
 * from the experiment manifest).  Sessions without an entry (base station,
 * planning sims, hand-made) belong to neither mode. */
export type SessionModes = Record<string, string>;

/** The session's launch mode, if it has one. */
export const sessionModeOf = (s: string | null | undefined, modes: SessionModes | undefined): string | undefined => (s ? modes?.[s] : undefined);

/** A session of the *other* launch mode: a mapping session in relocalize mode
 * never loads the prior; the backend refuses it. */
export function sessionMismatch(s: string | null | undefined, mode: string, modes: SessionModes | undefined): boolean {
  const m = sessionModeOf(s, modes);
  return !!m && m !== mode;
}

/** The robot session picker's options: only `mode`'s sessions, unless `showAll`
 * (or the backend sent no modes); the current value stays listed. */
export function sessionsForMode(all: string[], mode: string, modes: SessionModes | undefined, showAll: boolean, current?: string): string[] {
  if (showAll || !modes || !Object.keys(modes).length) return all;
  const own = all.filter(s => modes[s] === mode);
  return current && !own.includes(current) && all.includes(current) ? [...own, current] : own;
}

/** `s` when it fits `mode`, else `fallback` (the mode's preset). */
export const sessionForMode = (s: string | null | undefined, mode: string, modes: SessionModes | undefined, fallback: string): string =>
  s && !sessionMismatch(s, mode, modes) ? s : fallback;

/** Per-platform picks that fit `mode` (the others fall back to the mode's preset). */
export function picksForMode(picks: Record<string, string> | null | undefined, mode: string, modes: SessionModes | undefined): Record<string, string> {
  return Object.fromEntries(Object.entries(picks ?? {}).filter(([, s]) => s && !sessionMismatch(s, mode, modes)));
}
