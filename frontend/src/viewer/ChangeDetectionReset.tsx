// Fleet-wide change detection reset (API.md §19.3): every robot's detector, the
// AWCD base station's cached changes and the map side (plan repair's scene
// change writer, SGET) in one step. The dialog loads what the reset would do
// (GET api/change_detection/reset_all) and says it before anything runs:
// which robots are reset, which are skipped (offline / no detector) and what
// happens to the changes change detection already put in the map (kept by
// default; reverted when unchecked, or when the writer cannot keep them).
import {useState} from "react";
import {errorText, getJSON, postJSON} from "../lib/api";
import {Badge} from "../components/ui";

export interface ResetPlanRobot {
  robot: string;
  state: string | null;
  reachable: boolean;
  reason: string | null;
}

export interface ResetPlan {
  available: boolean;
  error?: string;
  robots: ResetPlanRobot[];
  map: {writer: string | null; running: boolean; keep_supported: boolean; reason: string | null};
}

export interface ResetResult {
  ok: boolean;
  error?: string;
  keep_applied: boolean;
  map: {action: "keep" | "revert" | "none"; ok: boolean; message?: string | null};
  robots: {robot: string; ok: boolean; skipped: boolean; message?: string | null; error?: string | null}[];
  base: {ok: boolean; message?: string; error?: string} | null;
  scene_repair?: {dropped: number};
}

/** One line per robot for the result. */
export function robotResultText(r: ResetResult["robots"][number]): string {
  if (r.skipped) return `${r.robot}: skipped (${r.error || "unreachable"})`;
  return r.ok ? `${r.robot}: reset` : `${r.robot}: FAILED (${r.error || r.message || "no answer"})`;
}

export function mapText(plan: ResetPlan, keep: boolean): string {
  const m = plan.map;
  if (!m.running) return `Map: no scene change writer running (${m.reason || "plan repair is off"}): nothing to keep or revert.`;
  if (keep) return "Map: changes change detection already applied to the map are KEPT; only pending, not yet applied detections are discarded.";
  return "Map: changes change detection already applied to the map are REVERTED (the map is rebuilt from the prior scene graph plus operator / chat edits).";
}

export function FleetChangeDetectionReset() {
  const [plan, setPlan] = useState<ResetPlan | null>(null);
  const [keep, setKeep] = useState(true);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ResetResult | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  const open = async () => {
    setBusy(true);
    setMsg(null);
    setResult(null);
    try {
      const p = await getJSON<ResetPlan>("api/change_detection/reset_all");
      if (!p.available) {
        setMsg(`Cannot reset: ${p.error || "ROS is off"}`);
        return;
      }
      setPlan(p);
      setKeep(p.map.keep_supported || !p.map.running);
    } catch (e) {
      setMsg(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  const run = async () => {
    if (!plan) return;
    setBusy(true);
    try {
      const keepApplied = plan.map.running ? keep : true;
      const r = await postJSON<ResetResult>("api/change_detection/reset_all", {keep_applied: keepApplied});
      setResult(r);
      setPlan(null);
    } catch (e) {
      setMsg(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  const reach = plan?.robots.filter(r => r.reachable) ?? [];
  const skip = plan?.robots.filter(r => !r.reachable) ?? [];
  const keepLocked = !!plan && plan.map.running && !plan.map.keep_supported;
  return (
    <div className="cd-fleet-reset small" data-testid="cd-fleet-reset">
      {!plan && (
        <button className="btn btn-xs btn-danger-outline" disabled={busy} onClick={() => void open()} data-testid="cd-reset-all"
          title="Reset change detection on every robot, at the AWCD base station and in plan repair's scene edits (SGET)">
          {busy ? "…" : "Reset change detection (all robots)…"}
        </button>
      )}
      {plan && (
        <div className="cd-reset-confirm" data-testid="cd-reset-confirm">
          <div><strong>Reset change detection for the whole fleet?</strong></div>
          <div>
            Robots reset ({reach.length}): {reach.length ? reach.map(r => `${r.robot} (${r.state ?? "state not reported"})`).join(", ") : "none reachable"}
          </div>
          {skip.length > 0 && (
            <div className="tone-warn" data-testid="cd-reset-skipped">
              Skipped: {skip.map(r => `${r.robot} — ${r.reason}`).join("; ")}
            </div>
          )}
          <div>AWCD base station: every robot's cached changes are dropped (the change list empties).</div>
          {plan.map.running && (
            <label className="row gap-sm">
              <input type="checkbox" checked={keep} disabled={keepLocked} onChange={e => setKeep(e.target.checked)} data-testid="cd-reset-keep" />
              Keep the changes already applied to the map
            </label>
          )}
          {keepLocked && <div className="tone-warn">This scene change writer cannot keep them (no keep_detections: update heracles), so they will be reverted.</div>}
          <div data-testid="cd-reset-map">{mapText(plan, keep)}</div>
          <div className="muted">Operator and chat (SGET) map edits are never touched. Pending replan recommendations caused only by change detection are dropped. Running detectors keep running.</div>
          <div className="row gap-sm">
            <button className="btn btn-xs btn-danger" disabled={busy} onClick={() => void run()} data-testid="cd-reset-go">
              {busy ? "…" : "Reset all"}
            </button>
            <button className="btn btn-xs" disabled={busy} onClick={() => setPlan(null)}>Cancel</button>
          </div>
        </div>
      )}
      {result && (
        <div data-testid="cd-reset-result">
          <Badge tone={result.ok ? "ok" : "warn"}>{result.ok ? "reset" : "reset with problems"}</Badge>{" "}
          {result.error && <span className="tone-err">{result.error}</span>}
          {result.map.message && <div>Map ({result.map.action}): {result.map.message}</div>}
          {result.robots.map(r => (
            <div key={r.robot} className={r.skipped ? "muted" : r.ok ? "" : "tone-err"}>{robotResultText(r)}</div>
          ))}
          {result.base && <div>Base station: {result.base.ok ? result.base.message || "cleared" : result.base.error || result.base.message}</div>}
        </div>
      )}
      {msg && <div className="tone-err">{msg}</div>}
    </div>
  );
}
