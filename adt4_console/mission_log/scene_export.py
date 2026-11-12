"""Export the mission's scene changes from Neo4j (read-only Cypher; plan §3/§5).

heracles_ros.change_log records every map change as a ``_SceneChange`` node
``{seq, source, op, data (JSON), key, active, time}``; ``_MapState`` holds the map
version / epoch.  At mission end the collector writes

* ``scene_changes.jsonl``: the changes with ``time`` in the mission window (plus
  every change after the last one before the mission, by ``seq``, so a change
  whose clock was off is not lost), ``data`` parsed, ordered by ``seq``;
* ``map_state.json``: ``_MapState`` (version, epoch, edits, rebuild state) and
  the ``seq`` range.

``run_read(query) -> {rows} | {error}`` is :func:`adt4_console.cypher_tools.run_read`
bound to a source: a READ transaction, so nothing here can write.
"""

from __future__ import annotations

import json
import pathlib

from . import write_json

MAX_CHANGES = 20000


def _num(v) -> str:
    return repr(float(v))


def export(run_read, t0: float, t1: float, out_dir, pad: float = 60.0) -> dict:
    out_dir = pathlib.Path(out_dir)
    lo, hi = float(t0) - pad, float(t1) + pad
    q = (
        "MATCH (c:_SceneChange) WHERE c.time >= "
        + _num(lo)
        + " AND c.time <= "
        + _num(hi)
        + " "
        "RETURN c.seq AS seq, c.source AS source, c.op AS op, c.data AS data, c.key AS key, "
        "c.active AS active, c.time AS time ORDER BY c.seq LIMIT " + str(MAX_CHANGES)
    )
    res = run_read(q)
    if "error" in res:
        return {"ok": False, "error": res["error"]}
    rows = list(res.get("rows") or [])
    before = run_read(
        "MATCH (c:_SceneChange) WHERE c.time < "
        + _num(lo)
        + " RETURN max(c.seq) AS seq"
    )
    last_before = None
    if "error" not in before and before.get("rows"):
        last_before = before["rows"][0].get("seq")
    if rows and last_before is not None:
        lo_seq = min(r["seq"] for r in rows if r.get("seq") is not None)
        if lo_seq > last_before + 1:  # changes with odd clocks between the two
            gap = run_read(
                f"MATCH (c:_SceneChange) WHERE c.seq > {int(last_before)} AND c.seq < {int(lo_seq)} "
                "RETURN c.seq AS seq, c.source AS source, c.op AS op, c.data AS data, c.key AS key, "
                "c.active AS active, c.time AS time ORDER BY c.seq"
            )  # fmt: skip
            rows = list(gap.get("rows") or []) + rows if "error" not in gap else rows
    ms = run_read("MATCH (m:_MapState) RETURN properties(m) AS m LIMIT 1")
    map_state = (
        (ms.get("rows") or [{}])[0].get("m")
        if "error" not in ms
        else {"error": ms["error"]}
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "scene_changes.jsonl", "w") as f:
        for r in rows:
            d = r.get("data")
            if isinstance(d, str):
                try:
                    d = json.loads(d)
                except ValueError:
                    pass
            f.write(json.dumps({**r, "data": d}, default=str) + "\n")
    seqs = [r["seq"] for r in rows if r.get("seq") is not None]
    info = {
        "ok": True,
        "n": len(rows),
        "seq_range": [min(seqs), max(seqs)] if seqs else None,
        "last_seq_before": last_before,
        "window": [lo, hi],
        "map_state": map_state,
        "ops": _count(r.get("op") for r in rows),
        "sources": _count(r.get("source") for r in rows),
    }
    write_json(out_dir / "map_state.json", info)
    return info


def _count(values) -> dict:
    out: dict = {}
    for v in values:
        out[str(v)] = out.get(str(v), 0) + 1
    return out
