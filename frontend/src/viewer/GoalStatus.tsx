// A sent goal's live status under its chat card: one chip line (planner, then
// each robot's step), expanding to the per-robot timeline.
import {useEffect, useState} from "react";
import {Badge} from "../components/ui";
import {fmtClock} from "./goalStatusFmt";
import {followTrack, robotLine, stageLabel, stageTone, useGoalTracks, type GoalTrackView} from "../lib/goalTrack";

export function GoalStatus({id}: {id: string}) {
  useEffect(() => followTrack(id), [id]);
  const v = useGoalTracks(s => s.tracks[id]);
  if (v === undefined) return null;
  if (v === null) return <div className="small muted" data-testid="goal-status">status: waiting for the console…</div>;
  return <GoalStatusView v={v} />;
}

export function GoalStatusView({v}: {v: GoalTrackView}) {
  const [open, setOpen] = useState(false);
  const robots = Object.values(v.robots);
  const withPlan = robots.filter(r => r.plan_id);
  return (
    <div className="goal-status small" data-testid="goal-status">
      <div className="row gap-sm" style={{flexWrap: "wrap", alignItems: "center"}}>
        <Badge tone={stageTone(v.stage)} title={v.text}>
          {stageLabel(v.stage)}
        </Badge>
        {withPlan.length === 0 && <span className={v.stage === "plan_failed" ? "tone-err" : "muted"}>{v.text}</span>}
        {withPlan.map(r => (
          <span key={r.robot} className="row gap-sm" data-testid={`goal-status-${r.robot}`} title={r.plan_id ? `plan ${r.plan_id}` : ""}>
            <span className="mono">{r.robot}</span>
            <Badge tone={stageTone(r.stage)}>{stageLabel(r.stage)}</Badge>
            <span className={`tone-${stageTone(r.stage) === "err" ? "err" : stageTone(r.stage) === "warn" ? "warn" : "ok"}`}>{robotLine(r)}</span>
          </span>
        ))}
        <button className="btn btn-xs" onClick={() => setOpen(o => !o)} aria-expanded={open} data-testid="goal-status-toggle">
          {open ? "hide" : "details"}
        </button>
      </div>
      {open && (
        <div className="goal-status-timeline" data-testid="goal-status-timeline">
          <TimeLine title="goal" events={v.events} />
          {robots.map(r => (
            <TimeLine key={r.robot} title={`${r.robot}${r.plan_id ? ` · plan ${r.plan_id.slice(0, 8)} · ${r.n} action${r.n === 1 ? "" : "s"}${r.actions.length ? ` (${r.actions.join(", ")})` : ""}` : ` · ${r.text}`}`} events={r.events} />
          ))}
        </div>
      )}
    </div>
  );
}

function TimeLine({title, events}: {title: string; events: {t: number; text: string}[]}) {
  return (
    <div>
      <div className="mono">{title}</div>
      <ul style={{margin: "2px 0 4px 16px", padding: 0}}>
        {events.map((e, i) => (
          <li key={i}>
            <span className="muted mono">{fmtClock(e.t)}</span> {e.text}
          </li>
        ))}
      </ul>
    </div>
  );
}
