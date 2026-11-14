// "Reset map" in the mission chat: put the Neo4j map back to the map it was loaded
// from when a planning session is done, without reloading it. It undoes every map edit
// (newest first) and deletes the objects the chat, the SAM3 image search or an operator
// added (POST api/maps/clear_added). Seconds; keyframes, images and embeddings stay.
// Saved missions and their mock objects are not touched.
import {useCallback, useEffect, useState} from "react";
import {errorText, getJSON, postJSON} from "../lib/api";
import {refreshEdit} from "../stores/mapEdit";

export interface ClearStatus {
  target: string;
  added_objects: number;
  sam3_observations: number;
  map_edits: number;
}

export interface ClearResult extends ClearStatus {
  removed: {sym: string; class: string | null}[];
  observations: number;
  edits_undone: number;
  undo_error: string | null;
}

/** "neo4j:pennfused" -> "pennfused"; null for other sources. */
export const neo4jTarget = (src: string | null | undefined) => (src && src.startsWith("neo4j:") ? src.slice(6) : null);

export const pendingCount = (s: ClearStatus | null) => (s ? s.added_objects + s.map_edits + s.sam3_observations : 0);

export function clearSummary(r: ClearResult): string {
  const parts: string[] = [];
  if (r.removed.length) parts.push(`removed ${r.removed.length} added object${r.removed.length === 1 ? "" : "s"}`);
  if (r.edits_undone) parts.push(`undid ${r.edits_undone} map edit${r.edits_undone === 1 ? "" : "s"}`);
  if (!parts.length && r.observations) parts.push(`removed ${r.observations} SAM3 observation${r.observations === 1 ? "" : "s"}`);
  const head = parts.length ? `Map reset: ${parts.join(", ")}` : "Map was already clean";
  return r.undo_error ? `${head}; could not undo every edit: ${r.undo_error}` : head;
}

export function MapResetButton({target, pollMs = 20000}: {target: string | null; pollMs?: number}) {
  const [st, setSt] = useState<ClearStatus | null>(null);
  const [asking, setAsking] = useState(false);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ok: boolean; text: string} | null>(null);

  const refresh = useCallback(async () => {
    if (!target) return;
    try {
      setSt(await getJSON<ClearStatus>(`api/maps/clear_added?target=${encodeURIComponent(target)}`));
    } catch {
      setSt(null);
    }
  }, [target]);
  useEffect(() => {
    void refresh();
    if (!pollMs) return;
    const id = window.setInterval(() => void refresh(), pollMs);
    return () => window.clearInterval(id);
  }, [refresh, pollMs]);

  if (!target) return null;
  const n = pendingCount(st);

  const go = async () => {
    setBusy(true);
    setNote(null);
    try {
      const r = await postJSON<ClearResult>("api/maps/clear_added", {target});
      setSt(r);
      setNote({ok: !r.undo_error, text: clearSummary(r)});
      void refreshEdit();
    } catch (e) {
      setNote({ok: false, text: errorText(e)});
    } finally {
      setBusy(false);
      setAsking(false);
    }
  };

  const what = st
    ? [
        st.added_objects ? `${st.added_objects} added object${st.added_objects === 1 ? "" : "s"}` : null,
        st.map_edits ? `${st.map_edits} map edit${st.map_edits === 1 ? "" : "s"}` : null,
        st.sam3_observations && !st.added_objects ? `${st.sam3_observations} SAM3 observation${st.sam3_observations === 1 ? "" : "s"}` : null,
      ].filter(Boolean)
    : [];

  return (
    <span className="map-reset small" data-testid="map-reset">
      {!asking && (
        <button
          className="btn btn-xs btn-danger-outline"
          disabled={busy || !st || n === 0}
          onClick={() => {
            setAsking(true);
            setNote(null);
          }}
          title={
            n
              ? `Put neo4j:${target} back to the loaded map: remove ${what.join(", ")}. Saved missions stay.`
              : "Nothing to reset: no objects added and no map edits since the map was loaded"
          }
          data-testid="map-reset-open"
        >
          ↺ Reset map{n ? ` (${n})` : ""}
        </button>
      )}
      {asking && (
        <span className="row gap-xs" data-testid="map-reset-confirm">
          <span>Remove {what.join(", ")} from the shared map?</span>
          <button className="btn btn-xs btn-danger" disabled={busy} onClick={() => void go()} data-testid="map-reset-go">
            {busy ? "Resetting…" : "Reset"}
          </button>
          <button className="btn btn-xs" disabled={busy} onClick={() => setAsking(false)}>
            Cancel
          </button>
        </span>
      )}
      {note && (
        <span className={note.ok ? "ok-msg" : "inline-err"} data-testid="map-reset-note">
          {" "}
          {note.text}
        </span>
      )}
    </span>
  );
}
