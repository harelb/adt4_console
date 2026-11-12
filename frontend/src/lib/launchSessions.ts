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
