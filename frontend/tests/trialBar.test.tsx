import {afterEach, describe, expect, it, vi} from "vitest";
import {act} from "react";
import {createRoot, type Root} from "react-dom/client";
import {TrialBar} from "../src/components/TrialBar";
import {useTrial, serverNow} from "../src/stores/trial";
import {fmtElapsed, fmtMinutes, machineTone, recordingCount, shortestLeft, staleAfter, type Trial, type TrialMachine, type TrialPayload} from "../src/lib/trial";

(globalThis as unknown as {IS_REACT_ACT_ENVIRONMENT: boolean}).IS_REACT_ACT_ENVIRONMENT = true;

const json = (o: unknown, status = 200) => new Response(JSON.stringify(o), {status, headers: {"Content-Type": "application/json"}});
const flush = () => act(async () => void (await new Promise(r => setTimeout(r, 0))));
const T0 = 1_794_800_000;

const machine = (name: string, over: Partial<TrialMachine> = {}): TrialMachine => ({
  name, role: "robot", state: "recording", message: "recording", bytes: 6e9, rate_bps: 80e6, free_bytes: 100e9,
  minutes_left: 20.8, last_seen: T0 + 118, warnings: [], topic_check: {ok: true, tries: 1, published: 20, topics: 20, missing: []}, ...over,
});
const trial = (machines: TrialMachine[], over: Partial<Trial> = {}): Trial => ({
  trial_id: "trial_20261115T193012Z_door-search", description: "Door search", state: "recording", started: T0, stopped: null,
  events: 9, flags: 2, machines: Object.fromEntries(machines.map(m => [m.name, m])), ...over,
});
const payload = (active: Trial | null, last: Trial | null = null): TrialPayload => ({now: T0 + 120, poll_s: 10, active, last});

describe("trial lib", () => {
  it("is green only for a fresh, warning-free recording", () => {
    const now = T0 + 120;
    expect(machineTone(machine("a"), now, 10)).toBe("ok");
    expect(machineTone(machine("a", {last_seen: now - staleAfter(10) - 1}), now, 10)).toBe("warn"); // answer too old
    expect(machineTone(machine("a", {last_seen: null}), now, 10)).toBe("warn");
    expect(machineTone(machine("a", {warnings: ["disk: 4.0 GB free"]}), now, 10)).toBe("warn");
    expect(machineTone(machine("a", {topic_check: {ok: true, missing: ["/a/fix"]}}), now, 10)).toBe("warn");
    expect(machineTone(machine("a", {topic_check: null}), now, 10)).toBe("warn"); // not checked yet
    expect(machineTone(machine("a", {topic_check: {ok: false, retrying: true, tries: 2}}), now, 10)).toBe("warn");
    expect(machineTone(machine("a", {state: "unreachable"}), now, 10)).toBe("warn");
    for (const state of ["lost", "failed", "stop_failed"] as const) expect(machineTone(machine("a", {state}), now, 10)).toBe("err");
  });
  it("counts only machines known to record now", () => {
    const t = trial([machine("a"), machine("b", {state: "unreachable"}), machine("c", {last_seen: T0}), machine("d", {state: "skipped"})]);
    expect(recordingCount(t, T0 + 120, 10)).toEqual({ok: 1, of: 3});
  });
  it("names the disk that fills first", () => {
    const t = trial([machine("a", {minutes_left: 43}), machine("b", {minutes_left: 8.2}), machine("c", {state: "lost", minutes_left: 1})]);
    expect(shortestLeft(t)).toEqual({name: "b", minutes: 8.2});
  });
  it("formats", () => {
    expect(fmtElapsed(75)).toBe("01:15");
    expect(fmtElapsed(3725)).toBe("1:02:05");
    expect(fmtMinutes(8.2)).toBe("8 min");
    expect(fmtMinutes(150)).toBe("2.5 h");
    expect(fmtMinutes(null)).toBe("?");
    expect(serverNow(payload(null), 1000, 6000)).toBe(T0 + 125);
  });
});

describe("TrialBar", () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;
  const mount = async (p: TrialPayload, fetchImpl?: (url: string, init?: RequestInit) => Response) => {
    vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => (fetchImpl ?? (() => json(p)))(String(url), init)));
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => root!.render(<TrialBar />));
    await flush();
    await act(async () => useTrial.getState().set(p));
  };
  const $ = (id: string) => host!.querySelector<HTMLElement>(`[data-testid="${id}"]`);
  afterEach(() => {
    act(() => root?.unmount());
    host?.remove();
    root = host = null;
    useTrial.setState({payload: null, receivedAt: 0});
    vi.unstubAllGlobals();
  });

  it("idle: offers to start and shows nothing green", async () => {
    await mount(payload(null));
    expect($("trial-bar")!.textContent).toContain("Not recording");
    expect($("trial-start")).not.toBeNull();
    expect($("trial-stop")).toBeNull();
  });

  it("after a stop: one line; warnings in the tooltip; chips only for a machine not confirmed stopped", async () => {
    const stopped = (name: string, over: Partial<TrialMachine> = {}) => machine(name, {state: "stopped", message: "stopped", ...over});
    await mount(payload(null, trial([stopped("euclid", {warnings: ["8 of 19 topics recorded no message"]}), stopped("hamilton")], {state: "stopped", stopped: T0 + 112})));
    expect($("trial-last")!.textContent).toContain("warnings on euclid");
    expect($("trial-last")!.title).toContain("euclid: 8 of 19 topics recorded no message");
    expect($("trial-machine-euclid")).toBeNull();
    act(() => root?.unmount());
    host?.remove();
    await mount(payload(null, trial([stopped("euclid"), machine("hamilton", {state: "stop_pending"})], {state: "stopped", stopped: T0 + 112})));
    expect($("trial-machine-hamilton")).not.toBeNull();
    expect($("trial-machine-euclid")).toBeNull();
  });

  it("recording: every machine listed with its state; one unknown machine turns the bar red", async () => {
    await mount(payload(trial([machine("euclid"), machine("hamilton")])));
    expect($("trial-count")!.textContent).toBe("2 of 2 recording");
    expect($("trial-bar")!.className).not.toContain("trial-bar-warn");
    expect($("trial-machine-euclid")!.textContent).toContain("6.0 GB");
    expect($("trial-machine-euclid")!.textContent).toContain("21 min left");
    expect($("trial-topics-euclid")!.textContent).toBe("20/20 topics");
    await act(async () => useTrial.getState().set(payload(trial([machine("euclid", {topic_check: {ok: false, retrying: true, tries: 2, error: "no answer"}}), machine("hamilton", {topic_check: {ok: true, tries: 1, published: 18, topics: 20, missing: ["/h/fix", "/h/rtcm"]}})]))));
    expect($("trial-topics-euclid")!.textContent).toBe("topic check got no answer, retrying (2)");
    expect($("trial-topics-hamilton")!.textContent).toBe("2/20 topics unpublished");
    // the topics nobody publishes: one per line in a panel on the chip, which a click pins open
    const pop = $("trial-missing-hamilton")!;
    expect([...pop.querySelectorAll(".trial-pop-list .mono")].map(e => e.textContent)).toEqual(["/h/fix", "/h/rtcm"]);
    expect(pop.textContent).toContain("2 of 20 topics on hamilton have no publisher");
    expect($("trial-missing-euclid")).toBeNull();
    await act(async () => $("trial-machine-hamilton")!.click());
    expect($("trial-machine-hamilton")!.className).toContain("trial-chip-pinned");
    await act(async () => [...pop.querySelectorAll("button")].find(b => b.textContent === "Close")!.click());
    expect($("trial-machine-hamilton")!.className).not.toContain("trial-chip-pinned");
    await act(async () => useTrial.getState().set(payload(trial([machine("euclid"), machine("hamilton")]))));

    await act(async () => useTrial.getState().set(payload(trial([machine("euclid"), machine("hamilton", {state: "unreachable", last_seen: T0 + 80})]))));
    expect($("trial-count")!.textContent).toBe("1 of 2 recording");
    expect($("trial-bar")!.className).toContain("trial-bar-warn");
    expect($("trial-machine-hamilton")!.textContent).toContain("no answer");
    expect($("trial-machine-hamilton")!.textContent).toContain("last answer 40 s ago");
  });

  it("a recorder that died is shown as NOT recording with a retry", async () => {
    await mount(payload(trial([machine("euclid", {state: "lost", message: "euclid is NOT recording"})])));
    expect($("trial-machine-euclid")!.textContent).toContain("NOT recording");
    expect($("trial-machine-euclid")!.querySelector("button")!.textContent).toBe("Retry");
  });

  it("a machine is removed after a second click, and a removed one is not counted", async () => {
    const posts: string[] = [];
    await mount(payload(trial([machine("euclid"), machine("gauss")])), (url, init) => {
      if (init?.method === "POST") posts.push(url);
      return json(payload(trial([machine("euclid"), machine("gauss", {state: "stopped", removed: true})])));
    });
    await act(async () => $("trial-remove-gauss")!.click());
    expect(posts).toEqual([]); // the first click only asks
    expect($("trial-machine-gauss")!.textContent).toContain("Stop recording on gauss?");
    await act(async () => $("trial-remove-yes-gauss")!.click());
    await flush();
    expect(posts.map(u => u.replace(/^.*api\//, ""))).toEqual(["trials/machines/gauss/stop"]);
    expect($("trial-count")!.textContent).toBe("1 of 1 recording");
    expect($("trial-bar")!.className).not.toContain("trial-bar-warn");
    expect($("trial-machine-gauss")!.textContent).toContain("removed");
    expect($("trial-remove-gauss")).toBeNull();
    await act(async () => $("trial-add-back-gauss")!.click());
    await flush();
    expect(posts.at(-1)).toContain("trials/machines/gauss/start");
    expect($("trial-add")).not.toBeNull();
  });

  it("the start dialog restarts a machine that is already recording, or just stops it", async () => {
    const pm = (name: string, over = {}) => ({name, role: "robot", ready: true, error: null, free_bytes: 100e9, total_bytes: 900e9, rate_bps: null, minutes: null, notes: [], recording: null, replaceable: false, ...over});
    let stray = true;
    const posts: {url: string; body: unknown}[] = [];
    await mount(payload(null), (url, init) => {
      if (init?.method === "POST") posts.push({url: url.replace(/^.*api\//, ""), body: JSON.parse(String(init.body ?? "{}"))});
      if (url.endsWith("stop-recorder")) { stray = false; return json({machine: "gauss", was_recording: true}); }
      if (url.endsWith("api/trials/preflight")) return json({machines: [pm("euclid"), stray ? pm("gauss", {ready: false, recording: "trial_old", replaceable: true, notes: ["already recording trial_old"]}) : pm("gauss")]});
      return json(payload(null));
    });
    await act(async () => $("trial-start")!.click());
    await flush();
    const box = document.querySelector('input[aria-label="Record on gauss"]') as HTMLInputElement;
    expect(box.disabled).toBe(false);
    expect(box.checked).toBe(true); // ticked: nothing is left out without the operator seeing it
    expect(document.body.textContent).toContain("already recording trial_old: that recording will be stopped");
    expect($("trial-start-confirm")!.textContent).toBe("Start recording on 2 machines (restarts 1)");
    const desc = $("trial-description") as HTMLInputElement;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(desc, "door search");
      desc.dispatchEvent(new Event("input", {bubbles: true}));
    });
    await act(async () => $("trial-start-confirm")!.click());
    await flush();
    expect(posts.find(p => p.url === "trials/start")).toEqual({url: "trials/start", body: {description: "door search", machines: ["euclid", "gauss"], replace: ["gauss"]}});
    // "Stop it now" closes the stray bag and asks the machines again
    await act(async () => $("trial-start")!.click());
    await flush();
    await act(async () => $("trial-stop-recorder-gauss")!.click());
    await flush();
    expect(posts.some(p => p.url === "trials/machines/gauss/stop-recorder")).toBe(true);
    expect($("trial-stop-recorder-gauss")).toBeNull();
    expect($("trial-start-confirm")!.textContent).toBe("Start recording on 2 machines");
  });

  it("a flag is posted, confirmed, and kept in the box when saving fails", async () => {
    const posts: string[] = [];
    let fail = false;
    await mount(payload(trial([machine("euclid")])), (url, init) => {
      if (url.endsWith("api/trials/flag")) {
        posts.push(String(init?.body));
        return fail ? json({detail: "the flag was NOT saved: events.jsonl: disk full"}, 500) : json({ok: true, event: {t: T0 + 75, elapsed_s: 75}});
      }
      return json(payload(trial([machine("euclid")])));
    });
    const input = $("trial-flag-input") as HTMLInputElement;
    const type = async (v: string) => {
      await act(async () => {
        Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, v);
        input.dispatchEvent(new Event("input", {bubbles: true}));
      });
    };
    await type("hamilton hesitated at the door");
    await act(async () => $("trial-flag-save")!.click());
    await flush();
    expect(JSON.parse(posts[0])).toEqual({text: "hamilton hesitated at the door"});
    expect($("trial-flag-status")!.textContent).toBe("flag saved at 01:15");
    expect(input.value).toBe("");

    fail = true;
    await type("second note");
    await act(async () => $("trial-flag-save")!.click());
    await flush();
    expect($("trial-flag-status")!.textContent).toContain("FLAG NOT SAVED");
    expect(input.value).toBe("second note");
  });

  it("stopping: offers Stop harder and Close only when a machine is stuck", async () => {
    await mount(payload(trial([machine("euclid", {state: "stopped"}), machine("gauss", {state: "stop_pending", last_seen: T0 + 60})], {state: "stopping"})));
    const text = $("trial-bar")!.textContent!;
    expect(text).toContain("STOPPING");
    expect(text).toContain("Close trial anyway");
    expect(text).not.toContain("Stop harder");
    expect($("trial-flag-input")).toBeNull();
  });
});

describe("full map", () => {
  it("hides the trial bar with the rest of the app chrome", async () => {
    const {readFileSync} = await import("node:fs");
    const css = readFileSync("src/styles.css", "utf8");
    const rule = css.slice(css.indexOf("body.map-full .sidenav"), css.indexOf("body.map-full .shell"));
    expect(rule).toContain("body.map-full .trial-bar");
    expect(rule).toContain("display: none");
  });
});
