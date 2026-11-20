// Layers -> Change detection: the AWCD base station's reconciled changes
// (awcd_base_station/reconciled_changes, on by default) per robot, and each
// robot's raw report (/awcd_changes, off by default; dashed on the map).
import {awcdRobots, changeVisible} from "../lib/awcd";
import {saveBool, setRobotHidden, useAwcd, useAwcdFeed} from "../stores/awcd";

export function AwcdLayerControls() {
  useAwcdFeed();
  const st = useAwcd(s => s.state);
  const show = useAwcd(s => s.show);
  const showRaw = useAwcd(s => s.showRaw);
  const hidden = useAwcd(s => s.hiddenRobots);
  if (!st || !st.available) return null;
  const robots = awcdRobots(st);
  const unattributed = st.changes.filter(c => !c.robots.length).length;
  const perRobot = (r: string) => st.changes.filter(c => c.robots.includes(r)).length;
  const rawCount = (r: string) => st.raw?.[r]?.changes.length ?? 0;
  const shown = st.changes.filter(c => changeVisible(c, hidden)).length;
  return (
    <div data-testid="awcd-layers">
      <header className="vsec-head">
        <h4>Change detection</h4>
      </header>
      <ul className="layer-list">
        <li>
          <label className="layer-row" title={`Merged changes of every robot, from ${st.topic ?? "awcd_base_station/reconciled_changes (not seen yet)"}`}>
            <input type="checkbox" checked={show} onChange={e => (saveBool("adt4.awcd.show", e.target.checked), useAwcd.setState({show: e.target.checked}))} />
            <span className="swatch" style={{background: "#00ff00"}} />
            <span className="layer-name">reconciled (base station)</span>
            <span className="mono small muted count-r">
              {shown}/{st.changes.length}
            </span>
          </label>
        </li>
        {robots.map(r => (
          <li key={r} className="layer-sub">
            <label className="layer-row" title={`Show the changes attributed to ${r}${showRaw ? ", and its raw report" : ""}`}>
              <input type="checkbox" checked={!hidden[r]} onChange={e => setRobotHidden(r, !e.target.checked)} />
              <span className="layer-name">↳ {r}</span>
              <span className="mono small muted count-r">
                {perRobot(r)}
                {showRaw ? ` · raw ${rawCount(r)}` : ""}
              </span>
            </label>
          </li>
        ))}
        {unattributed > 0 && (
          <li className="layer-sub">
            <label className="layer-row" title="Changes no robot's report names">
              <input type="checkbox" checked={!hidden["?"]} onChange={e => setRobotHidden("?", !e.target.checked)} />
              <span className="layer-name">↳ unattributed</span>
              <span className="mono small muted count-r">{unattributed}</span>
            </label>
          </li>
        )}
        <li>
          <label className="layer-row" title="Each robot's own report on /awcd_changes, in its frame (the old display), drawn dashed">
            <input type="checkbox" checked={showRaw} onChange={e => (saveBool("adt4.awcd.raw", e.target.checked), useAwcd.setState({showRaw: e.target.checked}))} />
            <span className="swatch line" style={{background: "#ffb020"}} />
            <span className="layer-name">raw robot reports (/awcd_changes)</span>
          </label>
        </li>
      </ul>
    </div>
  );
}
