// "Save map file" under the Areas list: writes the Neo4j map as it is now (with the
// regions drawn and saved to the map, and every other edit) to a scene graph file
// outside Neo4j: a map snapshot (Map › Snapshots) with the map's mesh, i.e.
// <snapshots dir>/<map>/<id>/dsg.json (spark_dsg; regions are Room nodes over their
// places) and extras.json (region outlines, names, edit metadata).
import {useState} from "react";
import {errorText} from "../lib/api";
import {withToken} from "../lib/auth";
import {editApi, type SnapshotInfo} from "../lib/mapEdit";
import {refreshEdit} from "../stores/mapEdit";

export const defaultFileName = (now = new Date()) =>
  `map with regions ${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")} ${String(now.getHours()).padStart(2, "0")}${String(now.getMinutes()).padStart(2, "0")}`;

export function SaveMapFile({src}: {src: string | null}) {
  const [name, setName] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState<SnapshotInfo | null>(null);
  const [err, setErr] = useState<string | null>(null);
  if (!src) return null;
  const save = async () => {
    setBusy(true);
    setErr(null);
    setSaved(null);
    try {
      const info = await editApi.snapshot(src, (name ?? "").trim() || defaultFileName(), {include_mesh: true});
      setSaved(info);
      setName(null);
      void refreshEdit();
    } catch (e) {
      setErr(errorText(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="save-map-file small" data-testid="save-map-file">
      <div className="row gap-xs wrap">
        {name === null ? (
          <button
            className="btn btn-xs"
            disabled={busy}
            onClick={() => setName(defaultFileName())}
            title="Write the map as it is now (regions and other edits included) to a scene graph file (dsg.json) outside Neo4j"
            data-testid="save-map-file-open"
          >
            {busy ? "Saving map file…" : "💾 Save map file"}
          </button>
        ) : (
          <>
            <input className="input input-xs grow" autoFocus value={name} onChange={e => setName(e.target.value)} onKeyDown={e => {
              if (e.key === "Enter") void save();
              if (e.key === "Escape") setName(null);
            }} aria-label="Map file name" />
            <button className="btn btn-xs btn-primary" disabled={busy} onClick={() => void save()} data-testid="save-map-file-go">
              {busy ? "Saving…" : "Save"}
            </button>
            <button className="btn btn-xs" disabled={busy} onClick={() => setName(null)}>
              Cancel
            </button>
          </>
        )}
      </div>
      {busy && <div className="muted">Writing the scene graph and its mesh; this takes a minute.</div>}
      {saved && (
        <div className="ok-msg" data-testid="save-map-file-done">
          Saved “{saved.name}” ({saved.counts?.Room ?? 0} regions, {saved.counts?.Object ?? 0} objects
          {saved.mesh ? ", with mesh" : ""}) to <span className="mono">{saved.dsg ?? saved.id}</span>{" "}
          <a href={withToken(editApi.downloadUrl(src, saved.id))} download>
            download .zip
          </a>
        </div>
      )}
      {err && <div className="inline-err">{err}</div>}
    </div>
  );
}
