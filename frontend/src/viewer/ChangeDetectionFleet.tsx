// Fleet-wide change detection Start / Stop (API.md §19.4): one click for every
// robot instead of one per robot card. Start is each robot's own Start (reset,
// then start) and keeps its gate: a robot that is not relocalized is skipped
// and named, and "Start anyway" (after a confirmation) overrides for those
// robots only. Unreachable robots and robots already running are skipped too;
// none of these is a failure. The summary comes from GET
// api/change_detection/fleet and follows each robot's latched state
// (robot/<robot>/change_detection). Reset is ChangeDetectionReset.tsx; the
// per-robot controls stay in the robot cards (a robot that joins late).
import {useEffect, useState} from "react";
import {errorText, getJSON, postJSON} from "../lib/api";
import {ws} from "../ws";
import {Badge} from "../components/ui";

export interface FleetRobot {
  robot: string;
  state: string | null;
  reachable: boolean;
  reason: string | null;
  relocalized?: {ok: boolean; reason: string};
}

export interface FleetState {
  available: boolean;
  error?: string;
  robots: FleetRobot[];
}

export interface FleetCmdResult {
  ok: boolean;
  cmd: "start" | "stop";
  robots: {robot: string; ok: boolean; skipped: boolean; not_relocalized?: boolean; message?: string | null; error?: string | null; state?: string | null}[];
  counts: {done: number; skipped: number; failed: number};
}

const ORDER = ["running", "paused", "reset", "not reported", "not relocalized", "unreachable"];

/** "3 running, 1 paused, 1 not relocalized": each robot counted once. */
export function fleetSummary(robots: FleetRobot[]): string {
  const n = new Map<string, number>();
  for (const r of robots) {
    const key = !r.reachable ? "unreachable" : r.state === "running" ? "running" : r.relocalized && !r.relocalized.ok ? "not relocalized" : r.state ?? "not reported";
    n.set(key, (n.get(key) ?? 0) + 1);
  }
  const keys = [...n.keys()].sort((a, b) => (ORDER.indexOf(a) + 1 || ORDER.length) - (ORDER.indexOf(b) + 1 || ORDER.length));
  return keys.map(k => `${n.get(k)} ${k}`).join(", ") || "no robots";
}

/** One line: who it was done for, who was skipped and why, who failed and why. */
export function fleetResultText(r: FleetCmdResult): string {
  const done = r.robots.filter(x => x.ok).map(x => x.robot);
  const skipped = r.robots.filter(x => x.skipped).map(x => `${x.robot} (${x.error || "skipped"})`);
  const failed = r.robots.filter(x => !x.ok && !x.skipped).map(x => `${x.robot} (${x.error || x.message || "no answer"})`);
  const verb = r.cmd === "start" ? "started" : "stopped";
  return [done.length ? `${verb} ${done.join(", ")}` : "", skipped.length ? `skipped ${skipped.join(", ")}` : "", failed.length ? `FAILED ${failed.join(", ")}` : ""].filter(Boolean).join(" · ") || "no robots";
}

export function FleetChangeDetectionControl() {
  const [fleet, setFleet] = useState<FleetRobot[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [result, setResult] = useState<FleetCmdResult | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [confirm, setConfirm] = useState(false);

  const load = () =>
    getJSON<FleetState>("api/change_detection/fleet").then(
      d => setFleet(d && d.available && Array.isArray(d.robots) ? d.robots : null),
      () => undefined,
    );
  useEffect(() => {
    void load();
  }, []);
  const names = (fleet ?? []).map(r => r.robot).join(",");
  useEffect(() => {
    const offs = names.split(",").filter(Boolean).map(robot =>
      ws.on(`robot/${robot}/change_detection`, d => {
        const state = (d as {state?: string | null} | null)?.state;
        if (state !== undefined) setFleet(prev => prev && prev.map(r => (r.robot === robot ? {...r, state} : r)));
      }),
    );
    return () => offs.forEach(off => off());
  }, [names]);

  const send = async (cmd: "start" | "stop", body: {robots?: string[]; override?: boolean} = {}) => {
    setBusy(cmd);
    setMsg(null);
    setConfirm(false);
    try {
      setResult(await postJSON<FleetCmdResult>(`api/change_detection/${cmd}_all`, body));
    } catch (e) {
      setResult(null);
      setMsg(errorText(e));
    } finally {
      setBusy(null);
      void load();
    }
  };

  if (!fleet) return null;
  const waiting = result?.cmd === "start" ? result.robots.filter(r => r.not_relocalized).map(r => r.robot) : [];
  return (
    <div className="cd-fleet small" data-testid="cd-fleet">
      <div className="row gap-sm wrap">
        <span className="muted">Change detection, all robots</span>
        <Badge tone={fleet.some(r => r.state === "running") ? "ok" : "off"}><span data-testid="cd-fleet-summary">{fleetSummary(fleet)}</span></Badge>
        <button className="btn btn-xs btn-primary" disabled={!!busy} onClick={() => void send("start")} data-testid="cd-start-all"
          title="Reset and start the detector of every robot that is relocalized and not running yet; the others are named">
          {busy === "start" ? "…" : "Start all"}
        </button>
        <button className="btn btn-xs" disabled={!!busy} onClick={() => void send("stop")} data-testid="cd-stop-all" title="Stop the detector of every reachable robot">
          {busy === "stop" ? "…" : "Stop all"}
        </button>
      </div>
      {result && <div className={result.ok ? "" : "tone-err"} data-testid="cd-fleet-result">{fleetResultText(result)}</div>}
      {waiting.length > 0 && (
        <div className="row gap-sm wrap cd-gate">
          {!confirm ? (
            <button className="btn btn-xs btn-danger-outline" disabled={!!busy} onClick={() => setConfirm(true)} data-testid="cd-start-anyway">
              Start anyway ({waiting.join(", ")})…
            </button>
          ) : (
            <>
              <span className="tone-warn">Changes detected before relocalization are placed in the wrong frame.</span>
              <button className="btn btn-xs btn-danger" disabled={!!busy} onClick={() => void send("start", {robots: waiting, override: true})} data-testid="cd-start-anyway-go">
                Start anyway
              </button>
              <button className="btn btn-xs" onClick={() => setConfirm(false)}>Cancel</button>
            </>
          )}
        </div>
      )}
      {msg && <div className="tone-err">{msg}</div>}
    </div>
  );
}
