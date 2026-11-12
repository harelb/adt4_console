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

/** The session a platform's picker shows: the user's pick, else the single-session
 * pick when it belongs to that platform, else the mode's preset. */
export function platformSession(platform: string, picked: Record<string, string>, session: string, presets: Record<string, string> | undefined): string {
  if (picked[platform]) return picked[platform];
  if (session && session.startsWith(platform)) return session;
  return presets?.[platform] ?? "";
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
