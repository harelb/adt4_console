// Planner state: what the planner believes before it plans the next goal --
// per robot what it holds (the map's HOLDS, which the heracles publisher puts
// in the scene graph the planner reads), its last pose in the map, whether it
// is in the current plan and how far (goal_manager robot_plan_states), the
// console's persistent constraints; for the team the map / planner versions
// and the last plan error. The operator can correct holding (through
// heracles_state_updater's update_holding_state), after a confirm.
import {useCallback, useEffect, useRef, useState} from "react";
import {errorText, getJSON, postJSON} from "../lib/api";
import {fmtTime} from "../lib/fuzzy";
import type {PlannerRobotState, PlannerStateSnapshot} from "../types";
import {Badge, type Tone} from "../components/ui";
import {ws} from "../ws";
import {useRobotColorOf} from "./planUtil";
import {factText} from "./PlanRepair";

export const PLANNER_STATE_PATH = "api/plan/planner_state";
export const PLANNER_STATE_TOPIC = "plan/planner_state";
const POLL_MS = 5000;

/** The snapshot: fetched on mount, on every hub "changed" push and every 5 s. */
export function usePlannerState(): {data: PlannerStateSnapshot | null; error: string | null; reload: () => void} {
  const [data, setData] = useState<PlannerStateSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const alive = useRef(true);
  const pending = useRef<number | undefined>(undefined);
  const reload = useCallback(() => {
    getJSON<PlannerStateSnapshot>(PLANNER_STATE_PATH).then(
      d => {
        if (!alive.current) return;
        // an older console (or a test stub) answers something else
        if (!d || !Array.isArray(d.robots) || !d.map || !d.planner) {
          setError("this console has no planner state");
          return;
        }
        setData(d);
        setError(null);
      },
      e => alive.current && setError(errorText(e)),
    );
  }, []);
  useEffect(() => {
    alive.current = true;
    reload();
    const id = window.setInterval(reload, POLL_MS);
    // pushes come in bursts (a plan touches several topics): fetch once per burst
    const off = ws.on(PLANNER_STATE_TOPIC, () => {
      window.clearTimeout(pending.current);
      pending.current = window.setTimeout(reload, 150);
    });
    return () => {
      alive.current = false;
      window.clearInterval(id);
      window.clearTimeout(pending.current);
      off();
    };
  }, [reload]);
  return {data, error, reload};
}

const STATUS_TONE: Record<string, Tone> = {running: "info", done: "ok", failed: "err", stalled: "warn", preempted: "off"};

/** "euclid holds O11 · hamilton —": the summary line. */
export function holdingSummary(robots: PlannerRobotState[]): string {
  if (!robots.length) return "no robots";
  return robots.map(r => `${r.robot} ${r.holding.length ? `holds ${r.holding.join(", ")}` : "—"}`).join(" · ");
}

export function planStateText(r: PlannerRobotState): string {
  const st = r.plan_state;
  if (!st) return r.in_plan ? "in plan" : "";
  const prog = st.total != null ? ` ${st.done ?? 0}/${st.total}` : "";
  return `${st.status}${prog}`;
}

/** Full view (Plan panel) or a collapsible summary (Chat panel). */
export function PlannerStatePanel({compact = false}: {compact?: boolean}) {
  const {data, error, reload} = usePlannerState();
  const robotsHolding = data?.robots.filter(r => r.holding.length) ?? [];
  const body = <PlannerStateBody data={data} error={error} reload={reload} />;
  if (!compact) return <section className="planner-state" aria-label="Planner state">{body}</section>;
  return (
    <details className="planner-state compact" data-testid="planner-state-compact">
      <summary className="small">
        <span className="muted">Planner believes: </span>
        <span className={robotsHolding.length ? "tone-warn" : ""}>{data ? holdingSummary(data.robots) : error ? "unavailable" : "…"}</span>
        {data?.last_error && <span className="tone-err"> · last plan error</span>}
      </summary>
      {body}
    </details>
  );
}

function PlannerStateBody({data, error, reload}: {data: PlannerStateSnapshot | null; error: string | null; reload: () => void}) {
  const colorOf = useRobotColorOf();
  const [editing, setEditing] = useState<string | null>(null);
  if (!data) return <div className="muted small">{error ? `Planner state unavailable: ${error}` : "Loading planner state…"}</div>;
  const {map, planner} = data;
  const epoch = (e: string | null) => (e ? e.slice(0, 8) : "—");
  return (
    <div className="planner-state-body">
      <div className="row gap between wrap">
        <h4 className="sub-title ps-title">Planner state</h4>
        <span className="mono small muted" title={`heracles map (Neo4j ${map.source ?? "?"}) version / epoch; the planner's count of maps it has seen / epoch`}>
          map v{map.version ?? "—"} · {epoch(map.epoch)} · planner v{planner.map_version ?? "—"} · {epoch(planner.map_epoch)}
          {map.epoch && planner.map_epoch && map.epoch !== planner.map_epoch && <span className="tone-warn"> (epochs differ)</span>}
        </span>
        <button className="btn btn-xs" onClick={reload} title="Refresh">
          ↻
        </button>
      </div>
      {map.error && <div className="inline-msg tone-warn small">Map: {map.error}</div>}
      {(map.rebuilding || map.rebuild_failed) && (
        <div className="inline-msg tone-warn small">{map.rebuild_failed ? `Map held after a failed load: ${map.rebuild_failed}` : "The map is being rebuilt; the planner keeps the last one."}</div>
      )}
      {!data.ros && <div className="inline-msg tone-warn small">ROS is not running in the console: plan states and holding changes are unavailable.</div>}
      {data.last_error && (
        <div className="inline-msg tone-err small ps-error" role="status">
          Last plan error{data.last_error.robot ? ` (${data.last_error.robot})` : ""}
          {data.last_error.t ? ` at ${fmtTime(data.last_error.t)}` : ""}: {data.last_error.text}
        </div>
      )}
      <table className="ps-table small">
        <thead>
          <tr>
            <th>Robot</th>
            <th title={data.holding_source}>Holds</th>
            <th title="goal_manager robot_plan_states (actions done / total); 'in plan': in omniplanner's last plan">Plan</th>
            <th title="Persistent constraints the console sends with every goal">Constraints</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {data.robots.map(r => (
            <RobotRow key={r.robot} r={r} color={colorOf(r.robot)} editing={editing === r.robot} onEdit={on => setEditing(on ? r.robot : null)} onDone={reload} canEdit={data.ros} />
          ))}
        </tbody>
      </table>
      {planner.plan_robots == null && <div className="muted small">No plan_robots from omniplanner yet{planner.goal_manager ? "" : "; no robot_plan_states from goal_manager"}.</div>}
      {data.corrections.length > 0 && (
        <ul className="result-list small ps-corrections">
          {data.corrections
            .slice(-3)
            .reverse()
            .map((c, i) => (
              <li key={i}>
                <span className="mono muted">{fmtTime(c.t)}</span> {c.author}: {c.request} <span className={c.ok ? "tone-ok" : "tone-err"}>{c.ok ? "done" : "failed"}</span>
              </li>
            ))}
        </ul>
      )}
    </div>
  );
}

function RobotRow({r, color, editing, onEdit, onDone, canEdit}: {r: PlannerRobotState; color: string; editing: boolean; onEdit: (on: boolean) => void; onDone: () => void; canEdit: boolean}) {
  const st = r.plan_state;
  const text = planStateText(r);
  return (
    <>
      <tr>
        <td>
          <span className="ps-dot" style={{background: color}} /> <span className="mono">{r.robot}</span>
          {r.position && (
            <div className="muted mono ps-pos" title="Last pose the state updater wrote to the map (the planner plans from the robot's pose; it keeps no at-* facts)">
              ({r.position[0].toFixed(1)}, {r.position[1].toFixed(1)})
            </div>
          )}
        </td>
        <td data-testid={`ps-holding-${r.robot}`}>
          {r.holding.length ? (
            r.holding.map(o => (
              <Badge key={o} tone="warn" title="The next plan for this robot starts with this object in hand (put down first unless the goal keeps it)">
                {o}
              </Badge>
            ))
          ) : (
            <span className="muted">nothing</span>
          )}
        </td>
        <td>
          {text ? <Badge tone={STATUS_TONE[st?.status ?? ""] ?? (r.in_plan ? "info" : "off")}>{text}</Badge> : <span className="muted">—</span>}
          {r.in_plan === false && st?.status === "running" && <span className="muted"> (not in last plan)</span>}
          {r.last_error && (
            <div className="tone-err ps-err" title={r.last_error.source}>
              {r.last_error.text}
            </div>
          )}
        </td>
        <td>{r.constraints.length ? r.constraints.map(c => <code key={factText(c)} className="mono ps-fact">{factText(c)}</code>) : <span className="muted">—</span>}</td>
        <td>
          <button className="btn btn-xs" disabled={!canEdit} onClick={() => onEdit(!editing)} aria-label={`Correct holding for ${r.robot}`} title={canEdit ? "Correct what the map says this robot holds" : "Needs ROS"}>
            {editing ? "Close" : "Correct"}
          </button>
        </td>
      </tr>
      {st?.conjuncts && st.conjuncts.length > 0 && (
        <tr className="ps-sub">
          <td />
          <td colSpan={4} className="muted mono">
            goal: {st.conjuncts.join(" ")}
            {st.remaining && st.remaining.length > 0 && <> · remaining: {st.remaining.join(", ")}</>}
          </td>
        </tr>
      )}
      {editing && (
        <tr className="ps-sub">
          <td colSpan={5}>
            <HoldingEditor robot={r.robot} holding={r.holding} onDone={() => {
              onEdit(false);
              onDone();
            }} />
          </td>
        </tr>
      )}
    </>
  );
}

/** Correct holding: "holds nothing" or "holds X", applied after a confirm. */
export function HoldingEditor({robot, holding, onDone}: {robot: string; holding: string[]; onDone?: () => void}) {
  const [mode, setMode] = useState<"nothing" | "object">(holding.length ? "nothing" : "object");
  const [obj, setObj] = useState("");
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const target = mode === "nothing" ? null : obj.trim();
  const request = target ? `${robot} holds ${target.toUpperCase()}` : `${robot} holds nothing`;
  const valid = mode === "nothing" || /^[A-Za-z]\s*\(?\s*\d+\s*\)?$/.test(obj.trim());
  const apply = async () => {
    setBusy(true);
    setErr(null);
    try {
      await postJSON(`${PLANNER_STATE_PATH}/holding`, {robot, object: target, confirm: true});
      setConfirming(false);
      onDone?.();
    } catch (e) {
      setErr(errorText(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="ps-editor small">
      <div className="row gap-sm wrap">
        <label className="row gap-xs">
          <input type="radio" name={`ps-hold-${robot}`} checked={mode === "nothing"} onChange={() => setMode("nothing")} aria-label={`${robot} holds nothing`} /> holds nothing
        </label>
        <label className="row gap-xs">
          <input type="radio" name={`ps-hold-${robot}`} checked={mode === "object"} onChange={() => setMode("object")} aria-label={`${robot} holds an object`} /> holds
        </label>
        <input
          className="input input-xs ps-obj"
          placeholder="O22"
          value={obj}
          onChange={e => {
            setObj(e.target.value);
            setMode("object");
          }}
          aria-label={`Object ${robot} holds`}
        />
        {!confirming ? (
          <button className="btn btn-xs btn-primary" disabled={!valid || busy} onClick={() => setConfirming(true)}>
            Apply…
          </button>
        ) : null}
      </div>
      {confirming && (
        <div className="inline-msg tone-warn ps-confirm" role="alertdialog" aria-label="Confirm holding change">
          Set “{request}” in the map{holding.length ? ` (now: ${holding.join(", ")})` : ""}? A released object stays where the map has it; the next plan
          starts from this.
          <div className="row gap-sm">
            <button className="btn btn-xs btn-primary" disabled={busy} onClick={() => void apply()}>
              Confirm
            </button>
            <button className="btn btn-xs" disabled={busy} onClick={() => setConfirming(false)}>
              Cancel
            </button>
          </div>
        </div>
      )}
      {err && <div className="inline-msg tone-err">{err}</div>}
    </div>
  );
}
