// "Save map file" (Areas): names the file, POSTs a snapshot with the mesh and
// shows where the dsg.json went.
import {afterEach, describe, expect, it, vi} from "vitest";
import {act} from "react";
import {createRoot, type Root} from "react-dom/client";
import {defaultFileName, SaveMapFile} from "../src/viewer/SaveMapFile";

(globalThis as unknown as {IS_REACT_ACT_ENVIRONMENT: boolean}).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let host: HTMLDivElement | null = null;
afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = host = null;
  vi.unstubAllGlobals();
});
async function render(el: React.ReactElement) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(el));
  return host;
}
const q = (h: HTMLElement, id: string) => h.querySelector(`[data-testid="${id}"]`) as HTMLButtonElement | null;

describe("Save map file", () => {
  it("default name", () => {
    expect(defaultFileName(new Date(2026, 10, 14, 9, 5))).toBe("map with regions 2026-11-14 0905");
  });

  it("hidden without a Neo4j map", async () => {
    const h = await render(<SaveMapFile src={null} />);
    expect(q(h, "save-map-file")).toBeNull();
  });

  it("saves a snapshot with the mesh and shows the dsg path", async () => {
    const calls: {url: string; body: string}[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        if (init?.method === "POST") calls.push({url: String(url), body: init.body as string});
        const info = {id: "20261114-1400-penn", name: "penn regions", created_at: 0, bytes: 1, author: null, mission: null,
          counts: {Room: 20, Object: 467}, map_version: 9, revision: 3, mesh: true, dir: "/data/snaps/pennfused/20261114-1400-penn",
          dsg: "/data/snaps/pennfused/20261114-1400-penn/dsg.json"};
        return new Response(JSON.stringify(init?.method === "POST" ? info : {}), {status: 200, headers: {"Content-Type": "application/json"}});
      }),
    );
    const h = await render(<SaveMapFile src="neo4j:pennfused" />);
    await act(async () => q(h, "save-map-file-open")!.click());
    const input = h.querySelector('input[aria-label="Map file name"]') as HTMLInputElement;
    expect(input.value).toMatch(/^map with regions /);
    await act(async () => q(h, "save-map-file-go")!.click());
    expect(calls[0].url).toContain("api/edit/neo4j%3Apennfused/snapshots");
    expect(JSON.parse(calls[0].body)).toMatchObject({include_mesh: true});
    const done = q(h, "save-map-file-done")!.textContent!;
    expect(done).toContain("20 regions, 467 objects, with mesh");
    expect(done).toContain("/data/snaps/pennfused/20261114-1400-penn/dsg.json");
  });
});
