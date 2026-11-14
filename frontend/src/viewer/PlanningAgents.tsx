// The mission planner's chat plans for the mission's own agents (Mission tab ->
// Agents), not the live fleet, so in place of "Planner believes" (the live robots'
// planner state) it shows those agents: name, kind and start. Simulated agents start
// empty-handed. Opening it shows each agent's start.
import {useMission, type Agent} from "../stores/mission";
import {agentColors} from "./MissionOverlay";
import {useRobotColorOf} from "./planUtil";

const fmt = (v: number) => (Math.abs(v) >= 100 ? v.toFixed(0) : v.toFixed(1));
export const startText = (a: Agent) => (a.start && a.start.length >= 2 ? `(${fmt(a.start[0])}, ${fmt(a.start[1])})` : "no start");

export function PlanningAgents() {
  const roster = useMission(s => s.roster);
  const colorOf = useRobotColorOf();
  const colors = agentColors(roster, colorOf);
  return (
    <details className="planner-state compact" data-testid="planning-agents">
      <summary className="small">
        <span className="muted">Planning with: </span>
        {roster.length ? (
          roster.map((a, i) => (
            <span key={a.id}>
              {i > 0 && <span className="muted"> · </span>}
              <span style={{color: colors[a.id]}}>●</span> {a.id}
            </span>
          ))
        ) : (
          <span className="tone-warn">no agents yet: add them in the Mission tab</span>
        )}
      </summary>
      <div className="planner-state-body small">
        {roster.length ? (
          <ul className="plain-list">
            {roster.map(a => (
              <li key={a.id} data-testid="planning-agent">
                <span style={{color: colors[a.id]}}>●</span> <b>{a.id}</b> <span className="muted">{a.kind}</span> · start <span className="mono">{startText(a)}</span> · holds nothing
              </li>
            ))}
          </ul>
        ) : null}
        <div className="muted">
          The chat plans for these simulated agents only (Mission tab → Agents). The live robots and what they hold are in the Robot view.
        </div>
      </div>
    </details>
  );
}
