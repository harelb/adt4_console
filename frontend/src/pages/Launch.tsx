import {useEffect, useMemo, useRef, useState} from "react";
import {useSearchParams} from "react-router";
import {parseLaunchPreset} from "../lib/launchLink";
import {defaultExperiment, isLaunchMode, lastPriorValue, nextExperimentName, type LastLaunch} from "../lib/launchMemory";
import {platformLabel, platformOf, platformSession, platformsOf, sessionsFor} from "../lib/launchSessions";
import {preferBaseCopy, priorAvailability} from "../lib/runCopies";
import {useFleet} from "../stores/fleet";
import {trackJob} from "../stores/jobs";
import {errorText, postJSON, useApi} from "../lib/api";
import {groupChecks, preflightLabel, preflightTone} from "../lib/preflight";
import type {ConsoleConfig, LaunchDefaults, LaunchPlan, MapEntry, MapsResponse, PreflightResult} from "../types";
import {Badge, Card, Dot, Empty, ErrorBox, Field, FuzzyPicker, Loading, PageHeader, Segmented, Spinner, Toggle} from "../components/ui";
import {fmtAgo} from "../lib/fuzzy";
import {jobIdOf} from "./Fleet";

type Mode = "mapping" | "relocalize";

interface PriorOption {
  machine: string;
  map: MapEntry;
}

export function LaunchPage() {
  // Maps → "Launch with map" preselects the prior map (and relocalize) through the URL
  const [search] = useSearchParams();
  const preset = useMemo(() => parseLaunchPreset(search), [search]);
  const [mode, setMode] = useState<Mode>(preset?.mode ?? "mapping");
  const defaults = useApi<LaunchDefaults>(`api/launch/defaults?mode=${mode}`);
  const config = useApi<ConsoleConfig>("api/config");
  const maps = useApi<MapsResponse>("api/maps");
  const machines = useFleet(s => s.machines);

  const [robots, setRobots] = useState<string[]>([]);
  const [session, setSession] = useState("");
  // platform -> session picked in the per-platform pickers (robots of 2+ platforms)
  const [platformPicks, setPlatformPicks] = useState<Record<string, string>>({});
  // launch only the base station's session (no robots)
  const [baseOnly, setBaseOnly] = useState(false);
  const [baseStation, setBaseStation] = useState<string>("");
  const [bsSession, setBsSession] = useState("");
  const [experiment, setExperiment] = useState(defaultExperiment("mapping"));
  const [record, setRecord] = useState(false);
  const [linkNet, setLinkNet] = useState(""); // "" = topology default
  const [linkMode, setLinkMode] = useState(""); // "" = topology default
  const [prior, setPrior] = useState<string>(preset ? `${preset.source}\u0000${preset.prior}` : ""); // "machine\u0000path"
  const [dry, setDry] = useState<{lines: string[]} | null>(null);
  const [busy, setBusy] = useState<"dry" | "launch" | "checks" | null>(null);
  const [checks, setChecks] = useState<PreflightResult | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [presetNote, setPresetNote] = useState<string | null>(null);
  const [copying, setCopying] = useState(false);
  const base = maps.data?.base || config.data?.local_machine || "";

  // Launch with map: a robot run with an in-sync copy here launches from that copy
  const presetApplied = useRef(false);
  useEffect(() => {
    // a new "Launch with map" link while this page is open: start from its prior
    presetApplied.current = false;
    if (preset) setPrior(`${preset.source}\u0000${preset.prior}`);
  }, [preset]);
  useEffect(() => {
    if (!preset || presetApplied.current || !maps.data) return;
    presetApplied.current = true;
    const {preset: p, note} = preferBaseCopy(preset, maps.data, base);
    if (p.source !== preset.source || p.prior !== preset.prior) setPrior(`${p.source}\u0000${p.prior}`);
    setPresetNote(note);
  }, [preset, maps.data, base]);

  // the last launch's choices (server-side, per base station): applied once, when the
  // page opens, so a relaunch after Stop & save keeps the prior map, sessions and robots
  const last = (defaults.data?.last ?? null) as LastLaunch | null;
  const lastApplied = useRef(false);
  const applyLast = (l: LastLaunch, m: Mode, withPrior: boolean) => {
    if (l.robots?.length) setRobots(l.robots);
    if (l.session) setSession(l.session);
    setPlatformPicks(l.sessions ?? {});
    setBaseOnly(!!l.base_only);
    setBaseStation(l.base_station ?? "");
    setBsSession(l.bs_session ?? "");
    setLinkNet(l.link_network ?? "");
    setLinkMode(l.link_mode ?? "");
    setRecord(!!l.record);
    if (withPrior) setPrior(lastPriorValue(l, base));
    setExperiment(nextExperimentName(l.experiment, m));
    setDry(null);
  };

  // a last launch in another mode is applied once that mode's defaults arrive
  const pendingLast = useRef<{l: LastLaunch; withPrior: boolean} | null>(null);
  const restoreLast = (l: LastLaunch, withPrior: boolean) => {
    const m = isLaunchMode(l.mode) ? l.mode : mode;
    if (m !== mode) {
      pendingLast.current = {l, withPrior};
      setMode(m);
    } else applyLast(l, m, withPrior);
  };

  // apply presets whenever the mode's defaults arrive
  useEffect(() => {
    const d = defaults.data;
    if (!d) return;
    const l = (d.last ?? null) as LastLaunch | null;
    if (!lastApplied.current && l) {
      lastApplied.current = true;
      if (!preset && isLaunchMode(l.mode) && l.mode !== mode) {
        pendingLast.current = {l, withPrior: true};
        setMode(l.mode); // reload that mode's defaults, then apply the last launch
        return;
      }
      pendingLast.current = {l, withPrior: !preset};
    }
    setRobots(d.robots ?? []);
    setSession(d.session ?? "");
    setPlatformPicks({});
    setBaseStation(d.base_station ?? "");
    setBsSession(d.bs_session ?? "");
    setDry(null);
    const p = pendingLast.current;
    if (p) {
      pendingLast.current = null;
      applyLast(p.l, mode, p.withPrior);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [defaults.data]);
  useEffect(() => setExperiment(defaultExperiment(mode)), [mode]);

  const sessions = defaults.data?.sessions ?? config.data?.sessions ?? [];
  const allMachines = machines?.machines ?? [];
  const robotMachines = allMachines.filter(m => m.role === "robot");
  const baseStations = allMachines.filter(m => m.role === "base_station");
  const robotPlatforms = defaults.data?.robot_platforms as Record<string, string> | undefined;
  const presets = defaults.data?.platform_sessions as Record<string, string> | undefined;
  const platformOfRobot = (r: string) => platformOf(r, robotPlatforms, allMachines.find(m => m.name === r)?.platform_type);
  const selPlatforms = platformsOf(robots, platformOfRobot);
  const perPlatform = selPlatforms.length > 1;
  const sessionOf = (p: string) => platformSession(p, platformPicks, session, presets);

  const priorOptions = useMemo<PriorOption[]>(() => {
    const out: PriorOption[] = [];
    for (const [machine, list] of Object.entries(maps.data?.machines ?? {})) {
      if (!Array.isArray(list)) continue;
      for (const m of list) if (m.path) out.push({machine, map: m});
    }
    return out.sort((a, b) => Number(Boolean(b.map.is_prior)) - Number(Boolean(a.map.is_prior)) || a.map.name.localeCompare(b.map.name));
  }, [maps.data]);

  const plan = (): LaunchPlan => {
    const [src, path] = prior ? prior.split("\u0000") : [null, ""];
    const sessions = perPlatform && !baseOnly ? Object.fromEntries(selPlatforms.map(p => [p, sessionOf(p)])) : {};
    return {
      robots: baseOnly ? [] : robots,
      // old backends: one session (the first robot's platform's)
      session: baseOnly ? "" : perPlatform ? sessions[selPlatforms[0]] : session,
      sessions,
      base_only: baseOnly,
      experiment: experiment.trim(),
      mode,
      prior_path: path ?? "",
      prior_source: src,
      record,
      base_station: baseStation || null,
      bs_session: baseStation ? bsSession || null : null,
      link_network: linkNet || null,
      link_mode: linkMode || null,
    };
  };

  const problems: string[] = [];
  if (baseOnly) {
    if (!baseStation) problems.push("Base station only: pick the base station");
    else if (!bsSession) problems.push("Base station only: pick the base station session");
  } else {
    if (!robots.length) problems.push("Select at least one robot (or Robots: none for the base station only)");
    if (perPlatform) {
      for (const p of selPlatforms) if (!sessionOf(p)) problems.push(`Pick a ${platformLabel(p)} session`);
    } else if (!session) problems.push("Pick a robot session");
  }
  if (!experiment.trim()) problems.push("Name the experiment");
  else if (!/^[\w.-]+$/.test(experiment.trim())) problems.push("Experiment name: letters, digits, . _ - only");
  if (mode === "relocalize" && !prior) problems.push("Relocalize needs a prior map");

  const doDry = async () => {
    setBusy("dry");
    setErr(null);
    try {
      setDry(await postJSON<{lines: string[]}>("api/launch/dry_run", plan()));
    } catch (e) {
      setDry(null);
      setErr(errorText(e));
    } finally {
      setBusy(null);
    }
  };
  const doChecks = async () => {
    setBusy("checks");
    setErr(null);
    try {
      setChecks(await postJSON<PreflightResult>("api/launch/preflight", plan()));
    } catch (e) {
      setChecks(null);
      setErr(errorText(e));
    } finally {
      setBusy(null);
    }
  };
  const doLaunch = async () => {
    setBusy("launch");
    setErr(null);
    try {
      const r = await postJSON("api/launch", plan());
      const id = jobIdOf(r);
      if (id) trackJob(id, baseOnly ? `Launch ${experiment} (base station only)` : `Launch ${experiment}`);
    } catch (e) {
      setErr(errorText(e));
    } finally {
      setBusy(null);
    }
  };

  const toggleRobot = (r: string) => setRobots(cur => (cur.includes(r) ? cur.filter(x => x !== r) : [...cur, r]));

  return (
    <div className="page">
      <PageHeader
        title="Launch"
        sub="Start the robot stack across the fleet, with an optional base station session and prior map."
        actions={
          <Segmented<Mode>
            ariaLabel="Launch preset"
            value={mode}
            onChange={setMode}
            options={[
              {value: "mapping", label: "Mapping"},
              {value: "relocalize", label: "Relocalize"},
            ]}
          />
        }
      />
      {last && (
        <div className="launch-last row gap wrap small" data-testid="launch-last">
          <span className="muted">Last launch{last.saved_at ? ` ${fmtAgo(last.saved_at)}` : ""}:</span>
          <span className="mono">{last.experiment || "?"}</span>
          {last.robots?.length ? <span className="mono">on {last.robots.join(", ")}</span> : null}
          <span>
            prior <span className="mono">{last.prior_path ? `${last.prior_source || base}:${last.prior_path.split("/").pop()}` : "none"}</span>
          </span>
          <button
            className="btn btn-xs"
            title="Fill the form with the last launch (same prior map, sessions, robots); the experiment name gets a new suffix"
            onClick={() => {
              restoreLast(last, true);
              setPresetNote(null);
            }}
          >
            Relaunch settings
          </button>
        </div>
      )}
      {defaults.error && <ErrorBox error={defaults.error} onRetry={defaults.reload} compact />}
      {defaults.loading && !defaults.data && <Loading label="Loading presets" />}
      <div className="launch-grid">
        <Card title="Robots">
          <Field label="Robots" hint={baseOnly ? "Only the base station's adt4_system session is (re)started; robots already running are left alone." : undefined}>
            <Segmented<"robots" | "none">
              ariaLabel="Robots"
              value={baseOnly ? "none" : "robots"}
              onChange={v => {
                setBaseOnly(v === "none");
                setDry(null);
                setChecks(null);
              }}
              options={[
                {value: "robots", label: "Selected robots"},
                {value: "none", label: "None (base station only)"},
              ]}
            />
          </Field>
          {!baseOnly && robotMachines.length === 0 && <Empty title="No robots in topology" />}
          {!baseOnly && (
          <>
          <div className="robot-pick">
            {robotMachines.map(m => (
              <label key={m.name} className={`pick ${robots.includes(m.name) ? "on" : ""} ${m.online ? "" : "offline"}`}>
                <input type="checkbox" checked={robots.includes(m.name)} onChange={() => toggleRobot(m.name)} />
                <Dot tone={m.online ? "ok" : "err"} />
                <span className="pick-name">{m.name}</span>
                <span className="muted small mono">{m.ip}</span>
                <span className="muted small">{platformLabel(platformOfRobot(m.name))}</span>
                {!m.online && <Badge tone="err">offline</Badge>}
              </label>
            ))}
          </div>
          {perPlatform ? (
            selPlatforms.map(p => (
              <Field key={p} label={`${platformLabel(p)} session`} hint={`Runs on ${robots.filter(r => platformOfRobot(r) === p).join(", ")}`}>
                <FuzzyPicker
                  ariaLabel={`${platformLabel(p)} session`}
                  value={sessionOf(p)}
                  options={sessionsFor(sessions, p)}
                  onChange={v => setPlatformPicks(cur => ({...cur, [p]: v}))}
                  placeholder="type to filter sessions"
                />
              </Field>
            ))
          ) : (
            <Field label="Robot session">
              <FuzzyPicker ariaLabel="Robot session" value={session} options={sessions} onChange={setSession} placeholder="type to filter sessions" />
            </Field>
          )}
          </>
          )}
        </Card>

        <Card title="Base station">
          <Field label="Machine">
            <select className="input" value={baseStation} onChange={e => setBaseStation(e.target.value)} aria-label="Base station">
              {!baseOnly && <option value="">none (robots only)</option>}
              {baseOnly && !baseStation && <option value="">pick a base station</option>}
              {baseStations.map(m => (
                <option key={m.name} value={m.name}>
                  {m.name} {m.online ? "" : "(offline)"} {m.local ? "(this machine)" : ""}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Base station session">
            <FuzzyPicker ariaLabel="Base station session" value={bsSession} options={sessions} onChange={setBsSession} placeholder="type to filter sessions" />
          </Field>
        </Card>

        <Card title="Experiment">
          <Field label="Experiment name" hint="Output goes to <output_root>/<name> on every machine.">
            <input className="input mono" value={experiment} onChange={e => setExperiment(e.target.value)} aria-label="Experiment name" />
          </Field>
          <Toggle checked={record} onChange={setRecord} label="Record rosbag" />
          <Field
            label="Robot link network"
            hint={`Robot <-> base traffic only; the console and SSH stay on ${defaults.data?.active_network ?? "this machine's network"}.`}
          >
            <select className="input" value={linkNet} onChange={e => setLinkNet(e.target.value)} aria-label="Robot link network">
              <option value="">topology default ({defaults.data?.link_network ?? "?"})</option>
              {(defaults.data?.link_networks ?? []).map(n => (
                <option key={n} value={n}>
                  {n}
                </option>
              ))}
            </select>
          </Field>
          <Field
            label="Robot link mode"
            hint={
              (linkMode || defaults.data?.link_mode) === "fleetlink"
                ? `Only fleetlink crosses the radio: each robot dials ${defaults.data?.fleetlink_base ?? "the fleetlink base"}; robot ROS graphs stay local.`
                : "Router mode: the base's zenoh router links to each robot's (ROS topics cross the radio)."
            }
          >
            <select className="input" value={linkMode} onChange={e => setLinkMode(e.target.value)} aria-label="Robot link mode">
              <option value="">topology default ({defaults.data?.link_mode ?? "?"})</option>
              {(defaults.data?.link_modes ?? ["router", "fleetlink"]).map(m => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
          </Field>
          <Field label={mode === "relocalize" ? "Prior map (required)" : "Prior map (optional)"}>
            {maps.error ? (
              <ErrorBox error={maps.error} compact onRetry={maps.reload} />
            ) : (
              <select className="input" value={prior} onChange={e => setPrior(e.target.value)} aria-label="Prior map">
                <option value="">none</option>
                {prior && !priorOptions.some(o => `${o.machine}\u0000${o.map.path}` === prior) && (
                  <option value={prior}>
                    {prior.split("\u0000")[0]}: {prior.split("\u0000")[1]} {maps.data ? "(not listed)" : ""}
                  </option>
                )}
                {priorOptions.map(o => (
                  <option key={o.machine + o.map.path} value={`${o.machine}\u0000${o.map.path}`}>
                    {o.map.is_prior ? "[prior] " : ""}
                    {o.machine}: {o.map.name} ({String(o.map.size)})
                  </option>
                ))}
              </select>
            )}
            {prior && <PriorHint maps={maps.data} prior={prior} base={base} note={presetNote} copying={copying} onCopy={async (machine, entry) => {
              setCopying(true);
              setErr(null);
              try {
                const store = entry.has_image_store;
                const r = await postJSON("api/maps/transfer", {src: machine, experiment: entry.name, images: store ? "keyframes" : "all", compression: store ? "fast" : "none"});
                const id = jobIdOf(r);
                if (id) trackJob(id, `Copy ${entry.name} to ${base}`);
                maps.reload();
              } catch (e) {
                setErr(errorText(e));
              } finally {
                setCopying(false);
              }
            }} />}
          </Field>
        </Card>
      </div>

      <div className="launch-bar">
        <div className="launch-summary">
          {problems.length ? (
            <span className="tone-warn">{problems[0]}</span>
          ) : (
            baseOnly ? (
            <span>
              <strong>{mode}</strong>: base station only, <span className="mono">{baseStation}</span> with <span className="mono">{bsSession}</span>
            </span>
            ) : (
            <span>
              <strong>{mode}</strong> on <span className="mono">{robots.join(", ")}</span> with{" "}
              <span className="mono">{perPlatform ? selPlatforms.map(p => `${platformLabel(p)}: ${sessionOf(p)}`).join(", ") : session}</span>
              {baseStation && (
                <>
                  {" "}
                  + <span className="mono">{baseStation}</span>
                </>
              )}
            </span>
            )
          )}
        </div>
        <button className="btn" onClick={doChecks} disabled={!!busy || (baseOnly ? !baseStation : !robots.length)} title="Read-only checks on every selected machine (also run at launch)">
          {busy === "checks" && <Spinner />} Run checks
        </button>
        <button className="btn" onClick={doDry} disabled={!!busy || problems.length > 0}>
          {busy === "dry" && <Spinner />} Dry run
        </button>
        <button className="btn btn-primary btn-lg" onClick={doLaunch} disabled={!!busy || problems.length > 0}>
          {busy === "launch" && <Spinner />} {baseOnly ? "Launch base station" : "Launch"}
        </button>
      </div>
      {err && <ErrorBox error={err} compact />}
      {checks && <PreflightPanel result={checks} order={[...(baseOnly ? [] : robots), baseStation].filter(Boolean)} onClear={() => setChecks(null)} />}
      {dry && (
        <Card title="Dry run: commands that would run" actions={<button className="btn btn-sm" onClick={() => setDry(null)}>Clear</button>}>
          <pre className="dryrun mono">{dry.lines.join("\n") || "(nothing to do)"}</pre>
        </Card>
      )}
    </div>
  );
}


export function PreflightPanel({result, order, onClear}: {result: PreflightResult; order: string[]; onClear: () => void}) {
  const groups = groupChecks(result.checks, order);
  return (
    <Card
      title={
        <>
          Preflight checks{" "}
          <Badge tone={result.nogo ? "err" : "ok"}>{result.nogo ? `${result.nogo} NO-GO` : "all GO"}</Badge>
        </>
      }
      actions={
        <>
          <span className="muted small">at {new Date(result.t * 1000).toLocaleTimeString()}</span>{" "}
          <button className="btn btn-sm" onClick={onClear}>
            Clear
          </button>
        </>
      }
    >
      <p className="muted small">Launch runs these again and asks you to confirm every NO-GO. The prior map is checked during the launch, after it is copied.</p>
      <div className="preflight-grid">
        {groups.map(([machine, cs]) => (
          <div key={machine} className={`preflight-machine pf-${preflightTone(result.verdicts[machine])}`} data-testid={`preflight-${machine}`}>
            <div className="preflight-head">
              <span className="pick-name">{machine}</span>
              <Badge tone={preflightTone(result.verdicts[machine])}>{preflightLabel(result.verdicts[machine])}</Badge>
            </div>
            <ul className="preflight-checks">
              {cs.map(c => (
                <li key={c.name + c.detail} className={`preflight-check status-${c.status}`}>
                  <Dot tone={preflightTone(c.status)} />
                  <span className="mono small">{c.name}</span>
                  <span className="small">{c.detail}</span>
                  {c.status !== "go" && c.hint && <span className="muted small preflight-hint">Fix: {c.hint}</span>}
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>
    </Card>
  );
}


function PriorHint({maps, prior, base, note, copying, onCopy}: {maps: MapsResponse | null | undefined; prior: string; base: string; note: string | null; copying: boolean; onCopy: (machine: string, entry: MapEntry) => void}) {
  const [machine, path] = prior.split("\u0000");
  const a = priorAvailability(maps, machine, path, base);
  return (
    <div className="prior-hint muted small">
      {note && <div>{note}</div>}
      {a.copy ? (
        <div>
          The viewer will show {base}'s copy <span className="mono">{a.copy}</span>{" "}
          <Badge tone={a.state === "in_sync" ? "ok" : "warn"}>{a.state === "in_sync" ? "in sync" : "outdated"}</Badge>
        </div>
      ) : !a.local && a.entry ? (
        <div className="row gap wrap prior-hint">
          <span className="tone-warn">Prior not on this machine yet: copy it so the viewer can show it.</span>
          <button className="btn btn-sm" disabled={copying} onClick={() => onCopy(machine, a.entry!)}>
            {copying && <Spinner />} Copy to {base || "base"}
          </button>
        </div>
      ) : null}
    </div>
  );
}
