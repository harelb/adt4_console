// Live tab: the session's mandatory relocalization phase and the mission controls
// (mission logging plan §2).  Each robot launched to relocalize is confirmed by the
// operator with the live evidence in view (hydra_multi accepted inter-robot loop
// closures, fleetlink's ROMAN prior-map matches, map -> odom); "Start mission"
// stays disabled until every session robot is relocalized or overridden.
import {useEffect, useState} from "react";
import {errorText} from "../lib/api";
import {Badge, type Tone} from "../components/ui";
import {logbookPost, startBlockedReason, useLogbook, wireLogbook, type RelocEvidence, type RobotReloc} from "../stores/logbook";

const STATE_TONE: Record<string, Tone> = {
  unlocalized: "warn",
  relocalizing: "info",
  relocalized: "ok",
  failed: "err",
  overridden: "warn",
  not_required: "off",
};
const STATE_TEXT: Record<string, string> = {
  unlocalized: "not relocalized",
  relocalizing: "relocalizing",
  relocalized: "relocalized",
  failed: "failed",
  overridden: "overridden",
  not_required: "not required (mapping)",
};

const fmtClock = (t?: number | null) => (t ? new Date(t * 1000).toLocaleTimeString() : "");
const fmtDur = (s: number) => (s < 90 ? `${Math.round(s)} s` : s < 5400 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1)} h`);

export function evidenceText(ev?: RelocEvidence): string {
  if (!ev) return "no evidence yet";
  const parts: string[] = [];
  parts.push(ev.hydra_total != null ? `hydra_multi ${ev.hydra_accepted ?? 0} / ${ev.hydra_total} accepted` : "hydra_multi: no count");
  parts.push(ev.roman_prior_lcs != null ? `${ev.roman_prior_lcs} ROMAN prior matches` : "ROMAN: no count");
  const T = ev.map_T_odom;
  parts.push(T && T.length === 7 ? `map→odom ${Math.hypot(T[0], T[1]).toFixed(1)} m` : "no map→odom");
  if (ev.live === false) parts.push("no live state");
  return parts.join(" · ");
}

function RelocRow({robot, r}: {robot: string; r: RobotReloc}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [override, setOverride] = useState(false);
  const [reason, setReason] = useState("");
  const act = async (action: string, body: Record<string, unknown> = {}) => {
    setBusy(action);
    setErr(null);
    try {
      await logbookPost(`reloc/${encodeURIComponent(robot)}/${action}`, body);
      setOverride(false);
      setReason("");
    } catch (e) {
      setErr(errorText(e));
    } finally {
      setBusy(null);
    }
  };
  const st = r.state;
  const ev = r.evidence;
  return (
    <div className="reloc-row small" data-testid={`relocalize-${robot}`}>
      <div className="row gap-sm wrap">
        <strong>{robot}</strong>
        <Badge tone={STATE_TONE[st] ?? "off"}>{STATE_TEXT[st] ?? st}</Badge>
        {ev?.verdict && st !== "not_required" && (
          <span className={ev.ok ? "tone-ok" : "muted"} title={ev.reason ?? undefined}>
            evidence: {ev.verdict}
          </span>
        )}
        {r.degraded && <Badge tone="warn">degraded {r.degraded.jump_m} m / {r.degraded.jump_deg}°</Badge>}
      </div>
      {st !== "not_required" && (
        <div className="muted mono" data-testid={`reloc-evidence-${robot}`}>
          {evidenceText(ev)}
        </div>
      )}
      {(st === "relocalized" || st === "overridden" || st === "failed") && (
        <div className="muted">
          {st === "overridden" ? `overridden: ${r.reason ?? ""}` : st === "failed" ? `failed${r.note ? `: ${r.note}` : ""}` : `confirmed ${fmtClock(r.t)}`}
          {r.operator ? ` by ${r.operator}` : ""}
        </div>
      )}
      <div className="row gap-sm wrap">
        {(st === "unlocalized" || st === "failed") && (
          <button className="btn btn-xs btn-primary" disabled={!!busy} onClick={() => void act("start")} data-testid={`reloc-start-${robot}`}
            title="Drive or walk the robot through the prior map until the evidence shows matches">
            Start relocalizing
          </button>
        )}
        {st === "relocalizing" && (
          <>
            <button className="btn btn-xs btn-primary" disabled={!!busy} onClick={() => void act("confirm", {method: "drive"})} data-testid={`reloc-confirm-${robot}`}
              title={ev?.ok ? "The evidence agrees" : `The evidence does not show a match yet: ${ev?.reason ?? "unknown"}`}>
              Confirm relocalized
            </button>
            <button className="btn btn-xs" disabled={!!busy} onClick={() => void act("fail")} data-testid={`reloc-fail-${robot}`}>
              Mark failed
            </button>
          </>
        )}
        {st !== "relocalized" && st !== "overridden" && st !== "not_required" && !override && (
          <button className="btn btn-xs btn-danger-outline" disabled={!!busy} onClick={() => setOverride(true)} data-testid={`reloc-override-${robot}`}>
            Override…
          </button>
        )}
        {override && (
          <>
            <input className="input input-sm" placeholder={`start without ${robot} relocalized because…`} value={reason}
              onChange={e => setReason(e.target.value)} aria-label={`override reason for ${robot}`} data-testid={`reloc-reason-${robot}`} />
            <button className="btn btn-xs btn-danger" disabled={!reason.trim() || !!busy} onClick={() => void act("override", {reason: reason.trim()})}
              data-testid={`reloc-override-confirm-${robot}`}>
              Override
            </button>
            <button className="btn btn-xs" onClick={() => setOverride(false)}>
              Cancel
            </button>
          </>
        )}
      </div>
      {err && <div className="tone-err">{err}</div>}
    </div>
  );
}

export function MissionControls() {
  const s = useLogbook(x => x.state);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now() / 1000), 5000);
    return () => window.clearInterval(id);
  }, []);
  const blocked = startBlockedReason(s);
  const m = s?.mission ?? null;
  const run = async (what: "start" | "end") => {
    setBusy(what);
    setErr(null);
    try {
      await logbookPost(`mission/${what}`, what === "start" ? {name: name.trim() || null} : {});
      if (what === "start") setName("");
    } catch (e) {
      setErr(errorText(e));
    } finally {
      setBusy(null);
    }
  };
  return (
    <div className="mission-controls small" data-testid="mission-controls">
      <div className="row gap-sm wrap">
        <span className="muted">mission</span>
        {m ? (
          <>
            <Badge tone="ok">open</Badge>
            <strong className="mono">{m.name || m.mission_id}</strong>
            <span className="muted">
              {fmtDur(Math.max(0, now - m.t_start))} · {m.goals.length} goal{m.goals.length === 1 ? "" : "s"}
              {m.start_reason === "first_goal" ? " · opened by the first goal" : ""}
            </span>
            <button className="btn btn-xs" disabled={!!busy} onClick={() => void run("end")} data-testid="mission-end">
              {busy === "end" ? "…" : "End mission"}
            </button>
          </>
        ) : (
          <>
            <Badge tone="off">none</Badge>
            <input className="input input-sm" placeholder="name (optional)" value={name} onChange={e => setName(e.target.value)} aria-label="Mission name" />
            <button className="btn btn-xs btn-primary" disabled={!!blocked || !!busy} onClick={() => void run("start")} title={blocked || "Start a mission (events and goals are grouped under it)"}
              data-testid="mission-start">
              {busy === "start" ? "…" : "Start mission"}
            </button>
          </>
        )}
      </div>
      {!m && blocked && (
        <div className="tone-warn" data-testid="mission-blocked">
          {blocked}
        </div>
      )}
      {err && <div className="tone-err">{err}</div>}
    </div>
  );
}

/** The session header, per-robot relocalization and the mission controls. */
export function SessionPanel() {
  useEffect(() => wireLogbook(), []);
  const s = useLogbook(x => x.state);
  if (!s) return null;
  const sess = s.session && s.session.state === "open" ? s.session : null;
  const robots = Object.entries(s.reloc ?? {});
  return (
    <section className="session-panel" data-testid="session-panel">
      <header className="row gap-sm wrap small">
        <strong>Session</strong>
        {sess ? (
          <span className="mono">
            {sess.session_id}
            {sess.experiment ? ` · ${sess.experiment}` : ""}
            {sess.mode ? ` · ${sess.mode}` : ""}
          </span>
        ) : (
          <span className="muted">none (launch from the console to start one)</span>
        )}
      </header>
      {sess && robots.length > 0 && (
        <div className="reloc-phase">
          <div className="muted small">Relocalization{sess.reloc_required ? " (required before a mission)" : ""}</div>
          {robots.map(([r, v]) => (
            <RelocRow key={r} robot={r} r={v} />
          ))}
        </div>
      )}
      <MissionControls />
    </section>
  );
}
