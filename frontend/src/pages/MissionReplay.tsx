// Mission replay (plan §7 tab 3, phase C2): a top-down map with a time scrubber.
// At time t: each robot's pose and trail (robot.pose), its active plan
// (plan.solved paths), the scene changes applied so far (scene_changes.jsonl),
// relocalization state and goal stage.  Play at 1x / 5x / 20x.  The map base is
// the mission's end-of-mission DSG snapshot (dsg_end.json), else the session's
// prior map (objects and places, top-down).
import {useEffect, useMemo, useRef, useState} from "react";
import {Badge, Segmented} from "../components/ui";
import {
  activePlans, changesUntil, fmtClock, goalStageAt, poseAt, relocAt, replayBounds, robotColor, trailUntil,
  type Replay,
} from "../lib/missionLog"; // prettier-ignore

const SPEEDS = ["1", "5", "20"] as const;
type Speed = (typeof SPEEDS)[number];

export function MissionReplay({replay}: {replay: Replay}) {
  const robots = useMemo(() => Object.keys(replay.poses).sort(), [replay]);
  const [t0, t1] = useMemo(() => {
    const a = replay.t_start ?? replay.t_range[0];
    const b = replay.t_end ?? replay.t_range[1];
    return [Math.min(a, replay.t_range[0] ?? a), Math.max(b, replay.t_range[1] ?? b)];
  }, [replay]);
  const [t, setT] = useState(t0);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState<Speed>("5");
  const last = useRef<number | null>(null);

  useEffect(() => setT(t0), [t0]);
  useEffect(() => {
    if (!playing) return;
    let raf = 0;
    const step = (now: number) => {
      const dt = last.current == null ? 0 : (now - last.current) / 1000;
      last.current = now;
      setT(cur => {
        const n = cur + dt * Number(speed);
        if (n >= t1) {
          setPlaying(false);
          return t1;
        }
        return n;
      });
      raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => {
      cancelAnimationFrame(raf);
      last.current = null;
    };
  }, [playing, speed, t1]);

  const [x0, y0, x1, y1] = useMemo(() => replayBounds(replay), [replay]);
  const w = x1 - x0;
  const h = y1 - y0;
  // world y up -> svg y down
  const sx = (x: number) => x - x0;
  const sy = (y: number) => y1 - y;
  const px = Math.max(w, h) / 300; // ~1 screen px at the default size
  const plans = activePlans(replay.plans, t);
  const changes = changesUntil(replay.changes, t);
  const reloc = relocAt(replay.reloc, t);
  const stage = goalStageAt(replay.stages, t);

  return (
    <div className="mreplay">
      <div className="mreplay-bar">
        <button className="btn btn-sm" onClick={() => (t >= t1 ? (setT(t0), setPlaying(true)) : setPlaying(p => !p))} aria-label={playing ? "Pause" : "Play"}>
          {playing ? "Pause" : "Play"}
        </button>
        <Segmented<Speed> value={speed} onChange={setSpeed} ariaLabel="Replay speed" options={SPEEDS.map(s => ({value: s, label: `${s}x`}))} />
        <input
          className="mreplay-scrub"
          type="range"
          min={t0}
          max={t1}
          step={0.1}
          value={t}
          aria-label="Replay time"
          onChange={e => {
            setPlaying(false);
            setT(Number(e.target.value));
          }}
        />
        <span className="mono" data-testid="replay-clock">
          {fmtClock(t, replay.t_start ?? t0)} / {fmtClock(t1, replay.t_start ?? t0)}
        </span>
      </div>
      <div className="mreplay-legend">
        {robots.map(r => (
          <span key={r} className="row gap">
            <span className="mreplay-swatch" style={{background: robotColor(robots, r)}} />
            <b>{r}</b>
            {reloc[r] && <Badge tone={reloc[r] === "relocalized" || reloc[r] === "not_required" ? "ok" : reloc[r] === "overridden" ? "warn" : "off"}>{reloc[r]}</Badge>}
            {stage[r] && <Badge tone="info">{stage[r]}</Badge>}
            {plans[r] && <span className="muted">plan {plans[r].len_m != null ? `${plans[r].len_m} m` : ""}</span>}
          </span>
        ))}
        <span className="muted">{changes.length} scene changes</span>
      </div>
      <svg className="mreplay-map" viewBox={`0 0 ${w} ${h}`} role="img" aria-label="Mission replay map">
        <g opacity={0.45}>
          {replay.map.places.map(([x, y], i) => (
            <circle key={`p${i}`} cx={sx(x)} cy={sy(y)} r={px * 1.2} fill="var(--line-2)" />
          ))}
          {replay.map.objects.map(([x, y, label], i) => (
            <rect key={`o${i}`} x={sx(x) - px * 2} y={sy(y) - px * 2} width={px * 4} height={px * 4} fill="var(--muted)">
              <title>{label ?? "object"}</title>
            </rect>
          ))}
        </g>
        {changes.filter(c => c.xy).map((c, i) => (
          <g key={`c${i}`} transform={`translate(${sx(c.xy![0])},${sy(c.xy![1])})`}>
            <circle r={px * 4} fill="none" stroke={c.op?.startsWith("remove") ? "var(--err)" : "var(--warn)"} strokeWidth={px * 1.5} />
            <title>{`${c.op} ${c.symbol ?? ""} ${c.label ?? ""} (${c.source ?? ""})`}</title>
          </g>
        ))}
        {robots.map(r => {
          const color = robotColor(robots, r);
          const p = plans[r];
          const trail = trailUntil(replay.poses[r], t);
          const pose = poseAt(replay.poses[r], t);
          return (
            <g key={r} data-robot={r}>
              {p && p.path.length > 1 && (
                <polyline points={p.path.map(([x, y]) => `${sx(x)},${sy(y)}`).join(" ")} fill="none" stroke={color} strokeWidth={px * 1.5} strokeDasharray={`${px * 4} ${px * 3}`} opacity={0.8} />
              )}
              {trail.length > 1 && <polyline points={trail.map(([x, y]) => `${sx(x)},${sy(y)}`).join(" ")} fill="none" stroke={color} strokeWidth={px * 2} opacity={0.6} />}
              {pose && (
                <g transform={`translate(${sx(pose.x)},${sy(pose.y)}) rotate(${-((pose.yaw ?? 0) * 180) / Math.PI})`}>
                  <circle r={px * 5} fill={color} stroke="var(--bg)" strokeWidth={px} />
                  <line x1={0} y1={0} x2={px * 9} y2={0} stroke={color} strokeWidth={px * 2} />
                </g>
              )}
            </g>
          );
        })}
      </svg>
      {!replay.map.objects.length && !replay.map.places.length && <div className="muted">No map snapshot (dsg_end.json) or prior map on this machine: robots and plans only.</div>}
    </div>
  );
}
