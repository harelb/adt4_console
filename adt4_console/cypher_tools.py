"""Cypher as the chat's interface to the scene graph (heracles_agents ``chatdsg``).

Reads run in a database-enforced READ transaction (Neo4j rejects a write there
with ``Neo.ClientError.Statement.AccessMode``); a write only runs after the
operator approves the exact query (``ChatService.execute_write``).  Results go
to the model as JSON-able rows: points become ``[x, y, z]``, nodes their
properties plus labels, capped at ``MAX_ROWS``.

The prompt's scene-graph and Cypher descriptions are heracles_agents'
(heracles_agents.prompts, added by chat.ChatService._system); this module adds
the console's system text, what its maps add (MAP_NOTES), and the schema read
live from the database so it cannot drift from what heracles writes.
"""

from __future__ import annotations

import re
import time

MAX_ROWS = 200
READ_TIMEOUT_S = 30.0
WRITE_TIMEOUT_S = 60.0
_SCHEMA_TTL_S = 60.0
# heracles bookkeeping nodes the model never needs
_HIDDEN_LABELS = {"_State", "_MapState", "_Schema", "GraphMutation", "Retired"}


def to_jsonable(v):
    """Neo4j driver values -> plain JSON values (points -> [x, y(, z)])."""
    from neo4j.graph import Node, Path, Relationship
    from neo4j.spatial import Point

    if isinstance(v, Point):
        return [round(float(c), 3) for c in v]
    if isinstance(v, Node):
        d = {k: to_jsonable(x) for k, x in v.items()}
        d["_labels"] = sorted(v.labels)
        return d
    if isinstance(v, Relationship):
        return {"_type": v.type, **{k: to_jsonable(x) for k, x in v.items()}}
    if isinstance(v, Path):
        return [to_jsonable(n) for n in v.nodes]
    if isinstance(v, dict):
        return {k: to_jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [to_jsonable(x) for x in v]
    if isinstance(v, float):
        return round(v, 4)
    if hasattr(v, "iso_format"):  # neo4j temporal types
        return v.iso_format()
    return v


def _driver(src):
    return src.db().driver


# A nodeSymbol compared with a string or a list of strings (property map, =, <>, IN).
_SYMBOL_CMP = re.compile(
    r"(nodeSymbol\s*(?::|=|<>|\bIN\b)\s*)(\[[^\]]*\]|'[^']*'|\"[^\"]*\")", re.I
)
_SYMBOL_LIT = re.compile(r"(['\"])([A-Za-z]\d+)\1")


def _other_case(sym: str) -> str:
    return sym[0].swapcase() + sym[1:]


def _stored_symbols(src, candidates) -> set:
    """Which of these node symbols the database holds, spelled exactly so."""
    import neo4j

    with _driver(src).session(default_access_mode=neo4j.READ_ACCESS) as s:
        res = s.run(
            "MATCH (n) WHERE n.nodeSymbol IN $c RETURN DISTINCT n.nodeSymbol AS s",
            c=sorted(candidates),
        )
        return {r["s"] for r in res}


def match_symbol_case(src, query: str) -> str:
    """The query with node symbols spelled the way this map stores them.

    The letter's case is part of the symbol and differs by layer and by map:
    objects are "O710", agents "a12", mesh places "T3349" on current Hydra maps
    and "t3349" on older ones. Operators type places as the planner prints
    them ("t3349"), and an exact match on the other spelling finds nothing.

    A symbol compared to ``nodeSymbol`` is respelled only when the map has no
    node with it as written and has one with the letter's case swapped; a
    symbol that exists as written (or in neither spelling) is left alone, so a
    map holding both ``p850`` and ``P850`` is queried as asked."""
    wanted = {
        lit[2] for cmp in _SYMBOL_CMP.finditer(query) for lit in _SYMBOL_LIT.finditer(cmp[2])
    }  # fmt: skip
    if not wanted:
        return query
    try:
        stored = _stored_symbols(src, wanted | {_other_case(w) for w in wanted})
    except Exception:  # noqa: BLE001 — the query then runs as written
        return query
    respell = {
        w: _other_case(w)
        for w in wanted
        if w not in stored and _other_case(w) in stored
    }
    if not respell:
        return query

    def fix(m):
        lits = _SYMBOL_LIT.sub(lambda s: s[1] + respell.get(s[2], s[2]) + s[1], m[2])
        return m[1] + lits

    return _SYMBOL_CMP.sub(fix, query)


def run_read(
    src, query: str, max_rows: int = MAX_ROWS, timeout_s: float = READ_TIMEOUT_S
) -> dict:
    """``{columns, rows, row_count, truncated}`` or ``{error}`` (sent back to the
    model so it can correct the query, like heracles_agents)."""
    import neo4j

    query = match_symbol_case(src, query)

    @neo4j.unit_of_work(timeout=timeout_s)
    def work(tx):
        res = tx.run(query)
        rows, truncated = [], False
        for i, rec in enumerate(res):
            if i >= max_rows:
                truncated = True
                break
            rows.append({k: to_jsonable(v) for k, v in rec.items()})
        return list(res.keys()), rows, truncated

    try:
        with _driver(src).session(default_access_mode=neo4j.READ_ACCESS) as s:
            cols, rows, truncated = s.execute_read(work)
    except Exception as e:  # noqa: BLE001 — the model sees the error and retries
        msg = str(e)
        if "AccessMode" in msg or "read access mode" in msg:
            msg = (
                "This query writes to the database; run_cypher is read-only. Use "
                "propose_cypher_write so the operator can approve it."
            )
        return {"error": msg[:1500]}
    return {
        "columns": cols,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
    }


def run_write(src, query: str, timeout_s: float = WRITE_TIMEOUT_S) -> dict:
    """Run an operator-approved write; ``{rows, counters}`` or ``{error}``."""
    import neo4j

    @neo4j.unit_of_work(timeout=timeout_s)
    def work(tx):
        res = tx.run(query)
        rows = [
            {k: to_jsonable(v) for k, v in rec.items()}
            for _, rec in zip(range(50), res)
        ]
        c = res.consume().counters
        counters = {
            k: getattr(c, k)
            for k in (
                "nodes_created", "nodes_deleted", "relationships_created",
                "relationships_deleted", "properties_set", "labels_added", "labels_removed",
            )
            if getattr(c, k, 0)
        }  # fmt: skip
        return rows, counters

    try:
        with _driver(src).session(default_access_mode=neo4j.WRITE_ACCESS) as s:
            rows, counters = s.execute_write(work)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:1500]}
    return {"rows": rows, "counters": counters}


# ------------------------------------------------------------------ prompt
SYSTEM = """\
You are the operator's assistant in a multi-robot mapping console. The scene graph \
lives in a Neo4j database, and Cypher is how you read it: you are an expert at \
mapping natural-language questions to Cypher queries for it.
- Answer questions about the map by calling run_cypher. If a query is invalid or \
returns nothing useful, correct the Cypher and retry (up to about 6 queries). \
Briefly say what you are looking up before each query.
- Node symbols in the database have NO parentheses or quotes: object O(12) is \
nodeSymbol "O12", mesh place P(850) is "P850". The letter's case is part of the symbol \
and depends on the layer and the map (the schema below shows one symbol per label): \
operators type mesh places as the planner prints them ("t3349") even when the map \
stores "T3349". Write symbols the way the schema shows them; show them to the \
operator as O(12) / P(850) / T(3349).
- Never write with run_cypher (it is read-only). To change the map, prefer \
propose_map_edit (reclassify / rename / move / remove / add objects, connect or \
disconnect nodes, mark places blocked, make a named region from a polygon or a set of \
nodes): the operator sees a diff card and approves or rejects it. Use \
propose_cypher_write (the exact query) only for changes those ops cannot express. Do \
not claim a change happened until you are told it was approved.
- To show what an object looks like, call show_object_images with its symbols.
- To find an object, look in the map FIRST: check "Object classes in the map" above \
for its label and synonyms (e.g. panda bear -> teddy bear, stuffed animal, toy) and \
run one run_cypher lookup by label / name (case-insensitive CONTAINS on the label and \
its synonyms). Only when the map has none (or the operator asks to look in the \
robot's images) call search_robot_images; do not try more than a couple of Cypher \
spellings. Tell the operator which you did ("found in the map" / "not in the map, \
searching the robot's images"). Image candidates are not in the map; the operator \
sees each candidate's image and approves or discards it.
- To send a robot somewhere or have it act on an object, call propose_goal; the \
operator approves goals in the UI. Never claim a robot is moving. Use kind='pddl' \
for everything the PDDL domain expresses (visit, inspect, pick up / hold, place, \
drag) with object/place symbols in lower case without parentheses, e.g. \
(visited-object o12), (holding euclid o1); kind='language' only for what PDDL \
cannot express.
- Robot names and platforms are in the fleet roster below: "spot" or "husky" is a \
platform, not a robot name (when the operator says "it's a spot", they mean the \
robot being discussed or the selected robot). Never guess a robot's platform or \
arm from its name: use the roster.
- Messages starting with [operator] report the operator's decisions in the UI (an \
approved or rejected write, an approved or discarded image candidate). Acknowledge \
them in one line and continue only what the operator asked for before (e.g. \
propose the goal to the object just added). Do NOT search the images again or \
repeat earlier tool calls because of such a note; the remaining candidates are \
still on screen and the operator may approve more of them.
- Keep answers short. When a count or distribution fits, an ASCII bar chart is welcome.
"""

# What the console's maps add to heracles_agents' scene graph and Cypher
# descriptions (heracles_agents.prompts, put before this in the prompt).
MAP_NOTES = """\
About the maps in this console (these notes win over the descriptions above):
- Node symbols in the database have NO parentheses or quotes ("O12", not "O(12)"). Mesh \
places can be P<n> (with a semantic class such as road or sidewalk) or T<n> / t<n> \
(traversability places, no class; upper or lower case), depending on the map; the schema below says which this map has. Place \
nodes (3D free space) are not planner targets: goals use MeshPlace and Object ids.
- Rooms are also the operator's named regions (class 'region', a name such as 'courtyard', \
a boundary polygon): a region CONTAINS the places inside it and those places CONTAIN the \
objects, so MATCH (r:Room {name: 'courtyard'})-[:CONTAINS*1..2]->(o:Object) lists its objects \
(always use *1..2: on fused maps objects hang off mesh places). Several Rooms can share a name \
(e.g. three 'intersection' rooms): tell them apart by their centre (r.center).
- Agent nodes (a<n>, or a robot-prefixed symbol) are trajectory keyframes of the robots; \
Observation nodes hold where/how an object was seen in a camera image: \
(o:Object)-[:HAS_OBSERVATION]->(:Observation).
- center is a 3D POINT in metres (map frame), NOT a list: its coordinates are center.x, \
center.y, center.z (center[1] is an error; results show it as [x, y, z]). North = +y, east = +x. \
Robot positions are given below when known: point({x: .., y: .., z: ..}).
- class is the semantic label name (see the labelspace).
- Objects may carry admission_status: trusted_prior / admitted are confirmed, candidate is \
unconfirmed, rejected was rejected. Leave out only rejected objects unless asked otherwise, \
null-safe (older maps have no admission_status): \
WHERE coalesce(o.admission_status, '') <> 'rejected'.
- is_active and is_predicted are Hydra bookkeeping (still in the active mapping window / \
predicted), NOT whether an object exists: never filter on them.
- Do not use apoc functions. Labels starting with _ are bookkeeping.
"""


class SchemaCache:
    """Live schema summary per source (labels, properties, relationships,
    labelspaces), refreshed every ``_SCHEMA_TTL_S``."""

    def __init__(self):
        self._cache: dict[str, tuple[float, str]] = {}

    def text(self, src) -> str:
        hit = self._cache.get(src.name)
        if hit and time.time() - hit[0] < _SCHEMA_TTL_S:
            return hit[1]
        try:
            txt = describe(src)
        except Exception as e:  # noqa: BLE001
            txt = f"(schema unavailable: {e})"
        self._cache[src.name] = (time.time(), txt)
        return txt


def describe(src) -> str:
    """What this database holds right now, for the prompt."""
    labels = run_read(
        src,
        "MATCH (n) UNWIND labels(n) AS l WITH l, count(*) AS c "
        "WHERE NOT l STARTS WITH '_' RETURN l, c ORDER BY c DESC",
        max_rows=60,
    )
    lines = ["Database contents now (label: count, properties):"]
    for r in labels.get("rows", []):
        if r["l"] in _HIDDEN_LABELS:
            continue
        keys = run_read(
            src,
            f"MATCH (n:`{r['l']}`) WITH n LIMIT 50 UNWIND keys(n) AS k RETURN DISTINCT k",
            max_rows=80,
        )
        props = ", ".join(sorted(x["k"] for x in keys.get("rows", [])))
        lines.append(f"- {r['l']}: {r['c']}  ({props}){_symbol_example(src, r['l'])}")
    rels = run_read(
        src,
        "MATCH ()-[r]->() RETURN type(r) AS t, count(*) AS c ORDER BY c DESC",
        max_rows=40,
    )
    if rels.get("rows"):
        lines.append(
            "Relationships: " + ", ".join(f"{x['t']} ({x['c']})" for x in rels["rows"])
        )
    ls = run_read(
        src,
        "MATCH (l:_Labelspace) RETURN l.layer AS layer, l.names AS names",
        max_rows=10,
    )
    for r in ls.get("rows", []):
        names = [n for n in (r.get("names") or []) if n and n != "unknown"]
        if names:
            lines.append(
                f"Labelspace ({r.get('layer')}): "
                + ", ".join(names)
                + ". Map synonyms to these labels."
            )
    # what is actually there (a labelspace name can have no objects)
    cls = run_read(
        src,
        "MATCH (o:Object) WHERE o.class IS NOT NULL RETURN o.class AS c, count(*) AS n ORDER BY n DESC",
        max_rows=200,
    )
    if cls.get("rows"):
        lines.append(
            "Object classes in the map (count; nothing else is mapped): "
            + ", ".join(f"{x['c']} ({x['n']})" for x in cls["rows"])
        )
    # names the operators gave nodes (Map tab): the words they will use for them
    named = run_read(
        src,
        "MATCH (n) WHERE (n:Object OR n:MeshPlace OR n:Place OR n:Room) AND n.name IS NOT NULL "
        "AND trim(n.name) <> '' AND toLower(n.name) <> toLower(coalesce(n.class, '')) "
        "RETURN n.nodeSymbol AS s, n.name AS name, n.class AS c ORDER BY s",
        max_rows=200,
    )
    if named.get("rows"):
        lines.append(
            "Names the operators gave map nodes (they may call them by these names): "
            + ", ".join(
                f"{_ui_sym(x['s'])} '{x['name']}'"
                + (f" ({x['c']})" if x.get("c") else "")
                for x in named["rows"]
            )
        )
    return "\n".join(lines)


def _symbol_example(src, label: str) -> str:
    """One node symbol of this label as stored, so the model writes its case."""
    one = run_read(
        src,
        f"MATCH (n:`{label}`) WHERE n.nodeSymbol IS NOT NULL "
        "RETURN n.nodeSymbol AS s LIMIT 1",
        max_rows=1,
    )
    rows = one.get("rows") or []
    return f'  symbols like "{rows[0]["s"]}"' if rows else ""


def _ui_sym(s) -> str:
    """``O12`` -> ``O(12)``."""
    m = re.match(r"^([A-Za-z])\(?(\d+)\)?$", str(s or ""))
    return f"{m.group(1)}({m.group(2)})" if m else str(s)
