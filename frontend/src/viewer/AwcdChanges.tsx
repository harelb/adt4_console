// The AWCD change list (Live tab): the base station's merged changes, a map
// overlay switch, Clear at the base station, click a change to fly to it.
// Starting / stopping detection is per robot (ChangeDetectionControl). Replan
// recommendations these changes cause are reviewed in the Plan tab.
import {useState} from "react";
import {errorText, postJSON} from "../lib/api";
import {changeColor, countsText, fmtClock, fmtPos, shortId, type AwcdChange} from "../lib/awcd";
import {saveBool, useAwcd, useAwcdFeed} from "../stores/awcd";
import {useViewer} from "../stores/viewer";
import {Badge, Toggle} from "../components/ui";

const MAX_ROWS = 200;

export function focusChange(c: AwcdChange) {
  useAwcd.setState({focus: c.key, show: true});
  useViewer.getState().fly([c.center[0], c.center[1], c.center[2]]);
}

export function AwcdChangesPanel() {
  useAwcdFeed();
  const st = useAwcd(s => s.state);
  const show = useAwcd(s => s.show);
  const labels = useAwcd(s => s.labels);
  const focus = useAwcd(s => s.focus);
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const [msg, setMsg] = useState<{tone: string; text: string} | null>(null);
  if (!st) return null;
  if (!st.available) {
    return (
      <section className="awcd-panel small muted" data-testid="awcd-off">
        Detected changes (AWCD): not available{st.error ? ` — ${st.error}` : ""}
      </section>
    );
  }
  const clear = async () => {
    setBusy(true);
    setMsg(null);
    try {
      const r = await postJSON<{ok: boolean; message?: string; error?: string}>("api/change_detection/clear", {robots: []});
      setMsg(r.ok ? {tone: "ok", text: r.message || "cleared"} : {tone: "err", text: r.error || r.message || "clear failed"});
    } catch (e) {
      setMsg({tone: "err", text: errorText(e)});
    } finally {
      setBusy(false);
      setConfirm(false);
    }
  };
  const reports = Object.entries(st.robots);
  return (
    <section className="awcd-panel" data-testid="awcd-panel">
      <header className="row gap between wrap">
        <span className="row gap-sm">
          <strong>Detected changes</strong>
          <span className="muted small">AWCD</span>
          <Badge tone={st.changes.length ? "warn" : "off"}>{countsText(st)}</Badge>
        </span>
        <span className="row gap-sm">
          <Toggle checked={show} onChange={v => (saveBool("adt4.awcd.show", v), useAwcd.setState({show: v}))} label="On map" title="Draw the changes on the map (green = added, red = removed), as RViz's AWCD Reconciled display" />
          <Toggle checked={labels} onChange={v => (saveBool("adt4.awcd.labels", v), useAwcd.setState({labels: v}))} label="Labels" />
        </span>
      </header>
      {!st.topic && <div className="small tone-warn">No AWCD base station seen (awcd_base_station/reconciled_changes): launch it with launch_awcd_base_station:=true.</div>}
      {reports.length > 0 && (
        <div className="small muted mono">
          reports: {reports.map(([r, s]) => `${r} +${s.added}/−${s.removed}`).join(" · ")}
        </div>
      )}
      {st.changes.length > 0 && (
        <ul className="result-list small awcd-list" data-testid="awcd-list">
          {st.changes.slice(0, MAX_ROWS).map(c => (
            <li key={c.key}>
              <button className={`awcd-row${focus === c.key ? " on" : ""}`} onClick={() => focusChange(c)} title="Show on the map">
                <span className="awcd-swatch" style={{background: changeColor(c.kind)}} />
                <span className={`awcd-kind awcd-${c.kind}`}>{c.kind}</span> <b>{c.label || "unknown"}</b> <span className="mono muted">#{shortId(c)}</span>{" "}
                <span className="muted">{c.robots.join(", ") || "?"}</span> <span className="mono muted">{fmtClock(c.stamp)}</span>{" "}
                <span className="mono muted">({fmtPos(c.center)})</span>
              </button>
            </li>
          ))}
          {st.changes.length > MAX_ROWS && <li className="muted">+{st.changes.length - MAX_ROWS} more</li>}
        </ul>
      )}
      <div className="row gap-sm wrap">
        {!confirm ? (
          <button className="btn btn-xs" disabled={busy || !st.topic} onClick={() => setConfirm(true)} title="Drop every robot's reported changes at the AWCD base station (clear_robots)">
            Clear at base station…
          </button>
        ) : (
          <>
            <span className="small">Drop all robots' changes at the base station?</span>
            <button className="btn btn-xs btn-danger" disabled={busy} onClick={() => void clear()} data-testid="awcd-clear">
              {busy ? "…" : "Clear"}
            </button>
            <button className="btn btn-xs" disabled={busy} onClick={() => setConfirm(false)}>
              Cancel
            </button>
          </>
        )}
        <button className="btn btn-xs" onClick={() => window.dispatchEvent(new CustomEvent("adt4-show-tab", {detail: "plan"}))} title="Replan recommendations these changes cause are confirmed or declined in the Plan tab">
          Replan review → Plan
        </button>
      </div>
      {msg && <div className={`small tone-${msg.tone}`}>{msg.text}</div>}
    </section>
  );
}
