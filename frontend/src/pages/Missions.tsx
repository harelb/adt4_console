// Missions page (mission logging plan §7, phase C1): recorded missions grouped by
// session, and one mission's Timeline / Goals / Replay / Chat / Images / Stats /
// Check / Export tabs over /api/logbook/missions.
import {useCallback, useEffect, useMemo, useState} from "react";
import {useNavigate, useParams} from "react-router";
import {errorText, getJSON, postJSON} from "../lib/api";
import {withToken} from "../lib/auth";
import {
  MISSIONS_API, PERIODIC, checkTone, collectTone, eventColor, fileUrl, fmtBytes, fmtClock, fmtDuration, fmtTime,
  laneOf, lanes, missionRel, resultTone,
  type ChatTurn, type EventPage, type GoalRecord, type MissionDetail, type MissionEvent, type MissionList,
  type Replay, type Stats,
} from "../lib/missionLog"; // prettier-ignore
import {logbookPost, startBlockedReason, useLogbook, wireLogbook} from "../stores/logbook";
import {Badge, Card, Empty, ErrorBox, KV, Loading, PageHeader, Segmented, Sparkline} from "../components/ui";
import {MissionReplay} from "./MissionReplay";

type Tab = "timeline" | "goals" | "replay" | "chat" | "images" | "stats" | "check" | "export";
const TABS: {value: Tab; label: string}[] = [
  {value: "timeline", label: "Timeline"},
  {value: "goals", label: "Goals"},
  {value: "replay", label: "Replay"},
  {value: "chat", label: "Chat"},
  {value: "images", label: "Images"},
  {value: "stats", label: "Stats"},
  {value: "check", label: "Check"},
  {value: "export", label: "Export"},
];

function useJSON<T>(path: string | null, deps: unknown[] = []): {data: T | null; error: unknown; reload: () => void} {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [n, setN] = useState(0);
  useEffect(() => {
    if (!path) return;
    const ac = new AbortController();
    getJSON<T>(path, ac.signal).then(
      d => {
        setData(d);
        setError(null);
      },
      e => {
        if (!ac.signal.aborted) setError(e);
      },
    );
    return () => ac.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, n, ...deps]);
  return {data, error, reload: useCallback(() => setN(x => x + 1), [])};
}

export function MissionsPage() {
  const {id} = useParams();
  return id ? <MissionDetailView id={id} /> : <MissionListView />;
}

// ------------------------------------------------------------------ list
function CurrentMission() {
  useEffect(() => wireLogbook(), []);
  const st = useLogbook(s => s.state);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<unknown>(null);
  const blocked = startBlockedReason(st);
  const act = async (path: string) => {
    setBusy(true);
    setErr(null);
    try {
      await logbookPost(path, {});
    } catch (e) {
      setErr(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="row gap">
      {st?.mission ? (
        <>
          <Badge tone="info">open: {st.mission.name || st.mission.mission_id}</Badge>
          <button className="btn btn-sm" disabled={busy} onClick={() => act("mission/end")}>End mission</button>
        </>
      ) : (
        <button className="btn btn-sm btn-primary" disabled={busy || !!blocked} title={blocked || "Start a mission now"} onClick={() => act("mission/start")}>
          Start mission
        </button>
      )}
      {blocked && !st?.mission && <span className="muted small">{blocked}</span>}
      {err != null && <span className="err small">{errorText(err)}</span>}
    </div>
  );
}

function MissionListView() {
  const {data, error, reload} = useJSON<MissionList>(MISSIONS_API);
  const navigate = useNavigate();
  return (
    <div className="page missions-page">
      <PageHeader title="Missions" sub="Recorded missions by session: collect status, check, timeline and replay" actions={<><CurrentMission /><button className="btn btn-sm" onClick={reload}>Refresh</button></>} />
      {error != null && <ErrorBox error={error} onRetry={reload} />}
      {!data && !error && <Loading label="Loading missions" />}
      {data && !data.sessions?.length && <Empty title="No missions yet">A console Launch starts a session; Start mission (or the first goal sent) opens a mission.</Empty>}
      {(data?.sessions ?? []).map(s => (
        <Card
          key={s.session_id ?? "none"}
          title={
            <span className="row gap">
              {s.session_id ? <span className="mono">{s.session_id}</span> : "Missions without a session"}
              {s.experiment && <span className="muted">{s.experiment}</span>}
              {s.mode && <Badge>{s.mode}</Badge>}
              {s.state && <Badge tone={s.state === "open" ? "info" : "off"}>{s.state}</Badge>}
            </span>
          }
        >
          {s.session_id && (
            <div className="row gap wrap msession-reloc">
              <span className="muted">{fmtTime(s.t_start)}</span>
              {Object.entries(s.reloc).map(([r, v]) => (
                <span key={r} title={[v.operator && `by ${v.operator}`, v.method, v.reason, v.note].filter(Boolean).join(" · ")}>
                  <b>{r}</b> <Badge tone={v.state === "relocalized" || v.state === "not_required" ? "ok" : v.state === "overridden" ? "warn" : "err"}>{v.state}</Badge>
                </span>
              ))}
            </div>
          )}
          <div className="table-wrap">
            <table className="table compact">
              <thead>
                <tr>
                  <th>Mission</th><th>Start</th><th>Duration</th><th>Robots</th><th>Goals</th><th>Check</th><th>Collect</th><th className="num">Size</th>
                </tr>
              </thead>
              <tbody>
                {s.missions.map(m => (
                  <tr key={m.mission_id} className="clickable" onClick={() => navigate(`/missions/${m.mission_id}`)} data-testid={`mission-row-${m.mission_id}`}>
                    <td>
                      <div>{m.name || <span className="mono">{m.mission_id}</span>}</div>
                      {m.name && <div className="mono muted small">{m.mission_id}</div>}
                    </td>
                    <td>{fmtTime(m.t_start)}</td>
                    <td>{m.state === "open" ? <Badge tone="info">open</Badge> : fmtDuration(m.duration_s)}</td>
                    <td>{m.robots.join(", ")}</td>
                    <td>
                      {m.n_goals} <span className="muted small">({m.goals_done} done{m.goals_failed ? `, ${m.goals_failed} failed` : ""})</span>
                    </td>
                    <td>{m.check ? <Badge tone={checkTone(m.check)}>{m.check}</Badge> : <span className="muted">–</span>}</td>
                    <td>{m.collect ? <Badge tone={collectTone(m.collect)}>{m.collect}</Badge> : <span className="muted">–</span>}</td>
                    <td className="num">{fmtBytes(m.size)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      ))}
    </div>
  );
}

// ------------------------------------------------------------------ detail
function MissionDetailView({id}: {id: string}) {
  const base = `${MISSIONS_API}/${encodeURIComponent(id)}`;
  const {data, error, reload} = useJSON<MissionDetail>(base);
  const [tab, setTab] = useState<Tab>("timeline");
  const navigate = useNavigate();
  // follow a running collect
  useEffect(() => {
    const st = data?.collect?.state;
    if (st !== "running" && st !== "deferred") return;
    const h = window.setTimeout(reload, 2000);
    return () => window.clearTimeout(h);
  }, [data, reload]);
  if (error) return <div className="page"><ErrorBox error={error} onRetry={reload} /></div>;
  if (!data) return <div className="page"><Loading label="Loading mission" /></div>;
  const s = data.summary;
  return (
    <div className="page missions-page">
      <PageHeader
        title={s.name || s.mission_id}
        sub={
          <span className="row gap wrap">
            <a href="#/missions" onClick={e => (e.preventDefault(), navigate("/missions"))}>Missions</a>
            <span className="mono">{s.mission_id}</span>
            <span>{fmtTime(s.t_start)} · {fmtDuration(s.duration_s)}</span>
            <span>{s.robots.join(", ")}</span>
            {s.check && <Badge tone={checkTone(s.check)}>check {s.check}</Badge>}
            {s.collect && <Badge tone={collectTone(s.collect)}>collect {s.collect}</Badge>}
            {s.end_reason && <span className="muted">ended: {s.end_reason}</span>}
          </span>
        }
        actions={<button className="btn btn-sm" onClick={reload}>Refresh</button>}
      />
      <Segmented<Tab> value={tab} onChange={setTab} options={TABS} ariaLabel="Mission tabs" />
      <div className="mtab">
        {tab === "timeline" && <TimelineTab base={base} detail={data} />}
        {tab === "goals" && <GoalsTab base={base} id={id} />}
        {tab === "replay" && <ReplayTab base={base} />}
        {tab === "chat" && <ChatTab base={base} />}
        {tab === "images" && <ImagesTab base={base} id={id} />}
        {tab === "stats" && <StatsTab base={base} />}
        {tab === "check" && <CheckTab base={base} detail={data} onChanged={reload} />}
        {tab === "export" && <ExportTab base={base} detail={data} />}
      </div>
    </div>
  );
}

function TimelineTab({base, detail}: {base: string; detail: MissionDetail}) {
  const [q, setQ] = useState("");
  const [machine, setMachine] = useState("");
  const [type, setType] = useState("");
  const [goal, setGoal] = useState("");
  const [periodic, setPeriodic] = useState(false);
  const [sel, setSel] = useState<MissionEvent | null>(null);
  const params = new URLSearchParams({limit: "5000"});
  if (q) params.set("q", q);
  if (machine) params.set("machine", machine);
  if (type) params.set("type", type);
  if (goal) params.set("goal", goal);
  const {data, error} = useJSON<EventPage>(`${base}/events?${params}`);
  const events = useMemo(() => (data?.events ?? []).filter(e => periodic || !PERIODIC.includes(e.type)), [data, periodic]);
  const t0 = detail.mission.t_start ?? data?.t_range[0] ?? 0;
  const t1 = Math.max(detail.mission.t_end ?? 0, data?.t_range[1] ?? t0 + 1);
  const ls = lanes(events);
  const span = Math.max(1, t1 - t0 + 60);
  const offsets = (detail.manifest?.clock_offsets ?? {}) as Record<string, {max_abs_s: number}>;
  return (
    <div>
      <div className="row gap wrap mfilters">
        <input placeholder="search payloads" value={q} onChange={e => setQ(e.target.value)} aria-label="Search events" />
        <select value={machine} onChange={e => setMachine(e.target.value)} aria-label="Machine">
          <option value="">all machines</option>
          {Object.keys(data?.facets.machine ?? {}).map(m => <option key={m} value={m}>{m}</option>)}
        </select>
        <select value={type} onChange={e => setType(e.target.value)} aria-label="Event type">
          <option value="">all types</option>
          {["goal.*", "plan.*", "action.*", "lease.*", "reloc.*", "loc.*", "chat.*", "mission.*"].map(t => <option key={t} value={t}>{t}</option>)}
          {Object.keys(data?.facets.type ?? {}).sort().map(t => <option key={t} value={t}>{t}</option>)}
        </select>
        <select value={goal} onChange={e => setGoal(e.target.value)} aria-label="Goal">
          <option value="">all goals</option>
          {(detail.mission.goals ?? []).map(g => <option key={g} value={g}>{g}</option>)}
        </select>
        <label className="row gap small"><input type="checkbox" checked={periodic} onChange={e => setPeriodic(e.target.checked)} /> periodic (pose, link, progress)</label>
        <span className="muted small">{data ? `${events.length} shown / ${data.total}` : ""}</span>
        {Object.entries(offsets).map(([r, o]) => (
          <Badge key={r} tone={o.max_abs_s > 2 ? "err" : o.max_abs_s > 0.25 ? "warn" : "ok"} title="robot clock - base clock (max |offset| in the mission); robot events are drawn in base time">
            {r} clock ±{(o.max_abs_s * 1000).toFixed(0)} ms
          </Badge>
        ))}
      </div>
      {error != null && <ErrorBox error={error} compact />}
      <div className="mtimeline" role="figure" aria-label="Mission timeline">
        {ls.map(l => (
          <div key={l} className="mlane">
            <div className="mlane-name">{l}</div>
            <div className="mlane-track">
              {events.filter(e => laneOf(e) === l).map((e, i) => (
                <button
                  key={`${e._machine}-${e.source}-${e.seq}-${i}`}
                  className={`mdot ${sel === e ? "on" : ""}`}
                  style={{left: `${(100 * (e.t_base - t0 + 30)) / span}%`, background: eventColor(e.type)}}
                  title={`${fmtClock(e.t_base, t0)} ${e.type}`}
                  aria-label={e.type}
                  onClick={() => setSel(e)}
                />
              ))}
            </div>
          </div>
        ))}
      </div>
      <div className="mevents">
        <div className="table-wrap mevents-list">
          <table className="table compact">
            <tbody>
              {events.slice(0, 1500).map((e, i) => (
                <tr key={i} className={`clickable ${sel === e ? "selected" : ""}`} onClick={() => setSel(e)}>
                  <td className="mono">{fmtClock(e.t_base, t0)}</td>
                  <td>{laneOf(e)}</td>
                  <td><span className="mswatch" style={{background: eventColor(e.type)}} /> {e.type}</td>
                  <td className="muted small">{e.goal_id ?? ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="mevent-detail">
          {sel ? (
            <>
              <KV k="time">{fmtTime(sel.t_base)} (+{fmtClock(sel.t_base, t0)})</KV>
              <KV k="source">{sel._machine} / {sel.source}</KV>
              <pre className="mono small">{JSON.stringify(sel.payload, null, 1)}</pre>
              <EventImages missionId={detail.summary.mission_id} payload={sel.payload} />
            </>
          ) : (
            <span className="muted">Click an event</span>
          )}
        </div>
      </div>
    </div>
  );
}

function refsOf(obj: unknown, out: string[] = []): string[] {
  if (Array.isArray(obj)) obj.forEach(v => refsOf(v, out));
  else if (obj && typeof obj === "object") {
    const o = obj as Record<string, unknown>;
    if (typeof o.$file === "string") out.push(o.$file);
    Object.values(o).forEach(v => refsOf(v, out));
  }
  return out;
}

function EventImages({missionId, payload}: {missionId: string; payload: unknown}) {
  const refs = refsOf(payload).filter(r => /\.(jpe?g|png|webp)$/i.test(r));
  if (!refs.length) return null;
  return (
    <div className="mimages">
      {refs.map(r => <img key={r} src={withToken(fileUrl(missionId, missionRel(missionId, r)))} alt={r} loading="lazy" />)}
    </div>
  );
}

function GoalsTab({base, id}: {base: string; id: string}) {
  const {data, error} = useJSON<{goals: GoalRecord[]}>(`${base}/goals`);
  if (error) return <ErrorBox error={error} />;
  if (!data) return <Loading label="Loading goals" />;
  if (!data.goals.length) return <Empty title="No goals in this mission" />;
  const t0 = data.goals[0]?.t_sent ?? 0;
  return (
    <div className="mgoals">
      {data.goals.map(g => (
        <Card key={g.goal_id} title={<span className="row gap"><span className="mono">{g.goal_id}</span><Badge>{g.kind ?? "goal"}</Badge><span>{g.robot}</span></span>}>
          <div className="mgoal">
            <div>
              <div className="muted small">card</div>
              <pre className="mono small">{JSON.stringify(g.card ?? g.payload ?? {}, null, 1)}</pre>
              {Object.entries(g.robots ?? {}).map(([r, v]) => (
                <div key={r} className="mgoal-robot">
                  <b>{r}</b> <Badge tone={resultTone(v.result)}>{v.result ?? "no outcome"}</Badge>
                  <KV k="time to reach">{fmtDuration(v.time_to_reach_s)}</KV>
                  <KV k="path planned / driven">{v.planned_m ?? "–"} m / {v.driven_m ?? "–"} m</KV>
                  <KV k="solve">{v.engine ?? ""} {v.solve_s != null ? `${v.solve_s} s` : "–"}</KV>
                  <KV k="repairs · leases · stalls">{v.repairs} · {v.leases} · {v.stalls}</KV>
                  <KV k="plan" mono>{v.plan_id}</KV>
                </div>
              ))}
            </div>
            <div>
              <div className="muted small">stages</div>
              <table className="table compact">
                <tbody>
                  {(g.stages ?? []).map((s, i) => (
                    <tr key={i}>
                      <td className="mono">{fmtClock(s.t, t0)}</td>
                      <td>{s.robot ?? "goal"}</td>
                      <td><Badge tone={resultTone(s.to)}>{s.to}</Badge></td>
                      <td className="muted small">{s.detail}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <div className="mimages">
                {(g.images ?? []).filter(x => x.$file).map(x => (
                  <img key={x.$file} src={withToken(fileUrl(id, missionRel(id, x.$file!)))} alt={x.sym ?? "goal image"} loading="lazy" />
                ))}
              </div>
            </div>
          </div>
        </Card>
      ))}
    </div>
  );
}

function ReplayTab({base}: {base: string}) {
  const {data, error} = useJSON<Replay>(`${base}/replay`);
  if (error) return <ErrorBox error={error} />;
  if (!data) return <Loading label="Loading replay" />;
  if (!Object.keys(data.poses).length) return <Empty title="No robot poses in this mission" />;
  return <MissionReplay replay={data} />;
}

function ChatTab({base}: {base: string}) {
  const {data, error} = useJSON<{turns: ChatTurn[]}>(`${base}/chat`);
  if (error) return <ErrorBox error={error} />;
  if (!data) return <Loading label="Loading chat" />;
  if (!data.turns.length) return <Empty title="No chat in this mission" />;
  return (
    <div className="mchat">
      {data.turns.map((c, i) => {
        const p = c.payload as Record<string, unknown>;
        if (c.type === "chat.user" || c.type === "chat.assistant")
          return (
            <div key={i} className={`mchat-msg ${c.type === "chat.user" ? "user" : "assistant"}`}>
              <span className="muted small">{fmtTime(c.t)}</span> {String(p.text ?? "")}
            </div>
          );
        if (c.type === "llm.call")
          return (
            <div key={i} className="mchat-meta muted small">
              LLM {String(p.resp_model ?? p.model ?? "?")} · {String(p.latency_ms ?? "?")} ms · {String(p.in_tok ?? "?")} in / {String(p.out_tok ?? "?")} out
              {p.cost_usd != null ? ` · $${Number(p.cost_usd).toFixed(4)}` : ""}
            </div>
          );
        return (
          <details key={i} className="mchat-tool">
            <summary className="small">{c.type} {String(p.name ?? "")}</summary>
            <pre className="mono small">{JSON.stringify(p, null, 1)}</pre>
          </details>
        );
      })}
    </div>
  );
}

function ImagesTab({base, id}: {base: string; id: string}) {
  const {data} = useJSON<{goals: GoalRecord[]}>(`${base}/goals`);
  const ev = useJSON<EventPage>(`${base}/events?type=sam3.*,op.manip,grasp.episode&limit=500`);
  const imgs = (data?.goals ?? []).flatMap(g => (g.images ?? []).filter(x => x.$file).map(x => ({goal: g.goal_id, ...x})));
  return (
    <div>
      <h4>Goal images</h4>
      {!imgs.length && <span className="muted">none</span>}
      <div className="mimages">
        {imgs.map(x => (
          <figure key={x.$file}>
            <img src={withToken(fileUrl(id, missionRel(id, x.$file!)))} alt={x.sym ?? ""} loading="lazy" />
            <figcaption className="small">{x.goal} · {x.sym}</figcaption>
          </figure>
        ))}
      </div>
      <h4>SAM3 queries, manipulation approvals, grasp episodes</h4>
      {!ev.data?.events.length && <span className="muted">none</span>}
      {ev.data?.events.map((e, i) => (
        <div key={i} className="mimg-event">
          <span className="mono small">{fmtTime(e.t_base)}</span> <b>{e.type}</b> <span className="muted small">{JSON.stringify(e.payload).slice(0, 160)}</span>
          <EventImages missionId={id} payload={e.payload} />
        </div>
      ))}
    </div>
  );
}

function StatsTab({base}: {base: string}) {
  const {data, error} = useJSON<Stats>(`${base}/stats`);
  if (error) return <ErrorBox error={error} />;
  if (!data) return <Loading label="Loading stats" />;
  return (
    <div className="mstats">
      <Card title="Goals" actions={<a className="btn btn-sm" href={withToken(`${base}/stats.csv`)} download>CSV</a>}>
        <div className="table-wrap">
          <table className="table compact">
            <thead>
              <tr><th>Goal</th><th>Robot</th><th>Result</th><th className="num">Solve s</th><th className="num">Planned m</th><th className="num">Driven m</th><th className="num">To reach</th><th className="num">Repairs</th><th className="num">Leases</th><th className="num">Stalls</th></tr>
            </thead>
            <tbody>
              {data.goals.map((g, i) => (
                <tr key={i}>
                  <td className="mono small">{g.goal_id}</td><td>{g.robot}</td><td><Badge tone={resultTone(g.result)}>{g.result ?? "–"}</Badge></td>
                  <td className="num">{g.solve_s ?? "–"}</td><td className="num">{g.planned_m ?? "–"}</td><td className="num">{g.driven_m ?? "–"}</td>
                  <td className="num">{fmtDuration(g.time_to_reach_s)}</td><td className="num">{g.repairs}</td><td className="num">{g.leases}</td><td className="num">{g.stalls}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
      <Card title="Robots">
        {Object.entries(data.robots).map(([r, v]) => (
          <KV key={r} k={r}>{v.driven_m} m driven · {v.goals} goals ({v.done} done, {v.failed} failed)</KV>
        ))}
      </Card>
      <Card title="LLM">
        <KV k="calls">{data.llm.calls}</KV>
        <KV k="tokens in / out">{data.llm.in_tok} / {data.llm.out_tok}</KV>
        <KV k="cost">${data.llm.cost_usd.toFixed(4)}</KV>
        <KV k="latency p50 / max">{data.llm.latency_ms_p50 ?? "–"} / {data.llm.latency_ms_max ?? "–"} ms</KV>
      </Card>
      <Card title="Link and clock">
        {Object.entries(data.link).map(([r, rows]) => (
          <div key={r} className="row gap">
            <b>{r}</b> latency <Sparkline values={rows.map(x => x[1] ?? 0)} /> loss <Sparkline values={rows.map(x => (x[3] ?? 0) * 100)} />
          </div>
        ))}
        {Object.entries(data.clock).map(([r, rows]) => (
          <KV key={r} k={`${r} clock offset`}>{rows.length} samples, last {rows.length ? `${rows[rows.length - 1][1]} ms` : "–"}</KV>
        ))}
      </Card>
    </div>
  );
}

function CheckTab({base, detail, onChanged}: {base: string; detail: MissionDetail; onChanged: () => void}) {
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState<unknown>(null);
  const run = async (what: "check" | "collect", body: unknown = {}) => {
    setBusy(what);
    setErr(null);
    try {
      await postJSON(`${base}/${what}`, body);
      onChanged();
    } catch (e) {
      setErr(e);
    } finally {
      setBusy("");
    }
  };
  const rep = detail.report;
  const machines = detail.collect?.machines ?? {};
  return (
    <div>
      <div className="row gap">
        <button className="btn btn-sm btn-primary" disabled={!!busy} onClick={() => run("check")}>Check</button>
        <button className="btn btn-sm" disabled={!!busy} onClick={() => run("collect")} title="Collect now: pull the robots' slices (skips the deferral while robots execute)">Re-collect</button>
        <button className="btn btn-sm" disabled={!!busy} onClick={() => run("collect", {full: true})} title="Also the mission bags and image stores (large)">Collect full</button>
        {rep && <Badge tone={checkTone(rep.status)}>{rep.status.toUpperCase()}</Badge>}
        {rep?.export_blocked && <Badge tone="err">export blocked</Badge>}
        {err != null && <span className="err small">{errorText(err)}</span>}
      </div>
      <div className="row gap wrap mcollect">
        {Object.entries(machines).map(([m, v]) => (
          <span key={m} title={v.error ?? v.why ?? ""}><b>{m}</b> <Badge tone={collectTone(v.state)}>{v.state}</Badge></span>
        ))}
      </div>
      {!rep && <Empty title="Not checked yet" />}
      {rep && (
        <table className="table compact mcheck">
          <tbody>
            {rep.checks.map(c => (
              <tr key={c.id} data-check={c.id}>
                <td><Badge tone={checkTone(c.status)}>{c.status}</Badge></td>
                <td><b>{c.title}</b><div className="muted small">{c.detail}</div>
                  {c.first && <details><summary className="small">first offending event</summary><pre className="mono small">{JSON.stringify(c.first, null, 1)}</pre></details>}
                  {c.items && c.status !== "pass" && <details><summary className="small">{c.n_items} items</summary><pre className="mono small">{JSON.stringify(c.items, null, 1)}</pre></details>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function ExportTab({base, detail}: {base: string; detail: MissionDetail}) {
  const [bags, setBags] = useState(false);
  const [busy, setBusy] = useState(false);
  const [res, setRes] = useState<{file: string; size: number} | null>(null);
  const [err, setErr] = useState<unknown>(null);
  const go = async () => {
    setBusy(true);
    setErr(null);
    try {
      setRes(await postJSON<{file: string; size: number}>(`${base}/export`, {bags}));
    } catch (e) {
      setErr(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div>
      <p className="muted">A .tar.zst of the mission folder (events, plans, images, manifest, report). The secret scan runs first and blocks the export on a hit.</p>
      <label className="row gap"><input type="checkbox" checked={bags} onChange={e => setBags(e.target.checked)} /> include the mission bags (only if collected with "Collect full")</label>
      <div className="row gap">
        <button className="btn btn-primary" disabled={busy || !!detail.report?.export_blocked} onClick={go}>Export</button>
        {res && <a className="btn" href={withToken(`${base}/export/${encodeURIComponent(res.file)}`)} download>{res.file} ({fmtBytes(res.size)})</a>}
        {err != null && <span className="err small">{errorText(err)}</span>}
      </div>
    </div>
  );
}

export default MissionsPage;
