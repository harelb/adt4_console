/** Compact Fleet-table text for the status poller's strings (fleet_helpers.parse_status);
 * the full string goes in the cell's tooltip. */

const missing = (v?: string | null) => v === undefined || v === null || v === "" || v === "N/A";

/** "99% (Not charging)" -> "99%", "40% (Charging)" -> "40% ⚡". */
export function shortBattery(b?: string | null): string | null {
  if (missing(b)) return null;
  const m = /(\d+(?:\.\d+)?)%/.exec(b!);
  if (!m) return b!;
  const charging = /\(\s*charging\s*\)/i.test(b!);
  return `${Math.round(Number(m[1]))}%${charging ? " ⚡" : ""}`;
}

/** "120G / 500G (76% used)" -> "120G". */
export function shortDisk(d?: string | null): string | null {
  if (missing(d)) return null;
  return d!.split("/")[0].trim() || d!;
}

/** "0.52/0.40/0.33" -> "0.52" (the 1-minute load). */
export function shortLoad(l?: string | null): string | null {
  if (missing(l)) return null;
  return l!.split("/")[0].trim();
}

/** "12% 1200/8000M" -> "12%" (GPU utilisation). */
export function shortGpu(g?: string | null): string | null {
  if (missing(g)) return null;
  return g!.split(" ")[0];
}
