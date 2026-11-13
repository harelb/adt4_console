// A video view that unmounts (side-tab switch, focus / camera change, drive
// layout) releases its stream with a grace period instead of stopping it, so a
// remount within the grace picks the running stream up again.
import {afterEach, beforeEach, describe, expect, it, vi} from "vitest";
import {act} from "react";
import {createRoot, type Root} from "react-dom/client";
import {VideoView} from "../src/viewer/VideoView";

(globalThis as unknown as {IS_REACT_ACT_ENVIRONMENT: boolean}).IS_REACT_ACT_ENVIRONMENT = true;

type Call = {url: string; body: Record<string, unknown>};

describe("VideoView stream lifecycle", () => {
  let calls: Call[];
  let host: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    calls = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        calls.push({url: String(url), body: init?.body ? JSON.parse(String(init.body)) : {}});
        return new Response(JSON.stringify({ok: true, params: {}, changed: false}), {status: 200, headers: {"Content-Type": "application/json"}});
      }),
    );
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
  });
  afterEach(() => {
    act(() => root.unmount());
    host.remove();
    vi.unstubAllGlobals();
  });

  const flush = async () => {
    for (let i = 0; i < 5; i++) await act(async () => await Promise.resolve());
  };
  const kinds = () => calls.map(c => c.url.split("/").pop());

  it("unmount sends a deferred stop (grace), never a plain stop; a remount starts again on the same stream", async () => {
    act(() => root.render(<VideoView key="a" robot="euclid" cam="rgb" />));
    await flush();
    expect(kinds()).toEqual(["start"]);
    expect(calls[0].url).toBe("api/robots/euclid/video/rgb/start");
    // the view unmounts (e.g. the operator switches side tab)
    act(() => root.render(<div />));
    await flush();
    expect(kinds()).toEqual(["start", "stop"]);
    expect(calls[1].body).toMatchObject({grace: true, viewer: calls[0].body.viewer});
    // ... and comes back: a start the console answers from the running stream
    act(() => root.render(<VideoView key="b" robot="euclid" cam="rgb" />));
    await flush();
    expect(kinds()).toEqual(["start", "stop", "start"]);
    expect(calls.filter(c => c.url.endsWith("/stop") && !c.body.grace)).toHaveLength(0);
  });

  it("switching camera releases the old stream with grace too", async () => {
    act(() => root.render(<VideoView key="rgb" robot="euclid" cam="rgb" />));
    await flush();
    act(() => root.render(<VideoView key="left" robot="euclid" cam="left" />));
    await flush();
    const stop = calls.find(c => c.url.endsWith("/stop"));
    expect(stop?.url).toBe("api/robots/euclid/video/rgb/stop");
    expect(stop?.body.grace).toBe(true);
    expect(calls.at(-1)?.url).toBe("api/robots/euclid/video/left/start");
  });
});
