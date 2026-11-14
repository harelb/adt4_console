// Bulk mission files: "Export all" bundles and several files at once.
import {afterEach, describe, expect, it, vi} from "vitest";
import {exportAllUrl, importMissionFiles, isBundle} from "../src/stores/mission";

afterEach(() => vi.unstubAllGlobals());

// a File's name and text() are all the import reads
const file = (name: string, data: unknown) => ({name, text: async () => (typeof data === "string" ? data : JSON.stringify(data))}) as unknown as File;

describe("bulk mission files", () => {
  it("tells a bundle from one mission", () => {
    expect(isBundle({format: "adt4-missions", missions: []})).toBe(true);
    expect(isBundle([{name: "a"}])).toBe(true);
    expect(isBundle({name: "a", tasks: []})).toBe(false);
    expect(exportAllUrl()).toContain("api/missions/export_all");
  });

  it("imports bundles and single files, reporting bad ones", async () => {
    const calls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        calls.push(String(url));
        const body = String(url).includes("import_all")
          ? {imported: [{id: "m2", name: "a"}, {id: "m3", name: "b"}], errors: [{index: 2, name: "bad", error: "not a valid mission"}]}
          : String(url).includes("api/missions/import")
            ? {mission: {id: "m4", name: "c", agents: [], tasks: []}, preview: {issues: []}}
            : {missions: []};
        return new Response(JSON.stringify(body), {status: 200, headers: {"Content-Type": "application/json"}});
      }),
    );
    const r = await importMissionFiles([file("all.json", {missions: [{}, {}, {}]}), file("one.json", {name: "c"}), file("junk.json", "{nope")]);
    expect(r.imported.map(m => m.id)).toEqual(["m2", "m3", "m4"]);
    expect(r.errors.map(e => e.name ?? e.file)).toEqual(["bad", "junk.json"]);
    expect(calls.filter(c => c.includes("import_all"))).toHaveLength(1);
  });
});
