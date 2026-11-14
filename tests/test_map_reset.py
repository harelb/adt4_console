"""One-click map reset (API.md §6.6): what it would undo, where it reloads from,
the place / room features, and the SAM3 reload call. A fake Neo4j target (no DB)."""

import numpy as np
import pytest
from fastapi import HTTPException

from adt4_console.routes import maps


class FakeTarget:
    name, uri = "penn", "neo4j://x:7699"

    def __init__(self, source):
        self.source, self.calls = source, []

    def query(self, cypher, **params):
        self.calls.append((cypher, params))
        if "_GraphMetadata" in cypher and "RETURN" in cypher:
            return [{"p": self.source}] if self.source else []
        if "sam3_created" in cypher:
            return [{"c": 2}]
        if "GraphMutation" in cypher:
            return [{"c": 1}]
        return []

    def count_nodes(self):
        return 18535


def _run(tmp_path):
    backend = tmp_path / "fused" / "hydra" / "backend"
    backend.mkdir(parents=True)
    (backend / "dsg_with_mesh.json").write_text("{}")
    return backend


def test_status_says_what_a_reset_undoes(tmp_path):
    backend = _run(tmp_path)
    st = maps.reset_status(FakeTarget(str(backend / "dsg_with_mesh.json")))
    assert (
        st["run"] == str(tmp_path / "fused")
        and st["added_objects"] == 2
        and st["map_edits"] == 1
    )
    assert st["nodes"] == 18535


def test_no_recorded_or_missing_source_is_a_409(tmp_path):
    for src in (None, str(tmp_path / "gone" / "dsg.json")):
        with pytest.raises(HTTPException) as e:
            maps.reset_status(FakeTarget(src))
        assert e.value.status_code == 409


def test_place_features_become_vector_properties(tmp_path):
    backend = _run(tmp_path)
    np.savez(
        backend / "semantic_features.npz",
        symbols=np.array(["P(12)", "R(3)"]),
        features=np.ones((2, 4), np.float32),
    )
    t = FakeTarget(None)
    assert maps.load_semantic_features(t, tmp_path / "fused") == 2
    sets = [p for c, p in t.calls if "SET n.siglip2_emb" in c]
    assert [r["s"] for r in sets[0]["rows"]] == ["P12", "R3"]
    assert sum("CREATE VECTOR INDEX" in c for c, _ in t.calls) == 2
    assert maps.load_semantic_features(t, tmp_path / "nothing") == 0


def test_routes_refuse_an_unknown_target(settings):
    from .conftest import make_client

    client, _ = make_client(settings)
    with client:
        assert (
            client.get("/api/maps/reset", params={"target": "nope"}).status_code == 404
        )
        assert (
            client.post("/api/maps/reset", json={"target": "nope"}).status_code == 404
        )


def test_sam3_reload_posts_to_the_server(monkeypatch):
    from adt4_console import sam3_client

    seen = {}
    monkeypatch.setattr(
        sam3_client,
        "_post",
        lambda base, path, payload, t: seen.update(path=path, payload=payload)
        or {"before": 1, "after": 2},
    )
    assert sam3_client.reload("http://127.0.0.1:8077") == {"before": 1, "after": 2}
    assert seen == {"path": "/reload", "payload": {}}


# --- Reset map in the mission chat: clear what a session added, no reload ---------------


class ClearTarget(FakeTarget):
    """Two added objects (one sharing an observation with a map object) and SAM3
    observations merged into a map object."""

    def __init__(self, added=True):
        super().__init__(None)
        self.added = added

    def query(self, cypher, **params):
        self.calls.append((cypher, params))
        if "RETURN o.nodeSymbol" in cypher:
            if not self.added:
                return []
            return [
                {"s": "O700", "c": "fire hydrant", "obs": ["e1", "e2", None]},
                {"s": "O704", "c": "box", "obs": []},
            ]
        if "count(ob)" in cypher:
            return [{"c": 3 if self.added else 0}]
        if "count(o)" in cypher or "count(g)" in cypher:
            return [{"c": 2 if self.added else 0}]
        return []


def test_clear_deletes_added_objects_and_sam3_observations():
    t = ClearTarget()
    out = maps.remove_added_objects(t)
    assert [r["sym"] for r in out["removed"]] == ["O700", "O704"]
    deletes = [(c, p) for c, p in t.calls if "DETACH DELETE" in c]
    assert deletes[0][1] == {"ids": ["e1", "e2"]}  # the added objects' own observations
    assert (
        maps.SAM3_OBS_WHERE in deletes[1][0]
    )  # every SAM3 observation, merged ones too
    assert maps.ADDED_WHERE in deletes[2][0] and "Object" in deletes[2][0]
    assert out["observations"] == 2 + 3
    # an observation a map object also has is kept: only added objects may share it
    assert "other <> o AND NOT (other.source IS NOT NULL" in t.calls[0][0]
    # nothing about keyframes, images or the map's own nodes
    assert not any(
        k in c
        for c, _ in t.calls
        for k in ("Agent", "SubKeyframe", "MeshPlace", "Place)")
    )


def test_clean_map_touches_nothing():
    t = ClearTarget(added=False)
    assert maps.remove_added_objects(t) == {"removed": [], "observations": 0}
    assert not any("DELETE" in c for c, _ in t.calls)


def test_status_counts_only_open_edits():
    st = maps.clear_status(ClearTarget())
    assert st == {
        "target": "penn",
        "added_objects": 2,
        "sam3_observations": 3,
        "map_edits": 2,
    }
    assert "undone_by IS NULL AND g.undo_of IS NULL" in maps.OPEN_EDITS


class FakeEdits:
    def __init__(self, n, fail_at=None):
        self.n, self.fail_at, self.calls = n, fail_at, []

    def undo(self, src_id, mutation_id, *, author=None, force=False):
        self.calls.append((src_id, mutation_id, force))
        if self.fail_at is not None and len(self.calls) == self.fail_at:
            raise RuntimeError("cannot undo edit-3 (step 1): node O9 is gone")
        if len(self.calls) > self.n:
            raise RuntimeError("nothing to undo")
        return {}


def test_undo_all_edits_newest_first_until_none_left():
    e = FakeEdits(3)
    assert maps.undo_all_edits(e, "neo4j:penn", {"operator": "op"}) == (3, None)
    assert all(
        c == ("neo4j:penn", None, True) for c in e.calls
    )  # latest each time, forced


def test_undo_all_edits_reports_the_edit_it_could_not_undo():
    n, err = maps.undo_all_edits(FakeEdits(5, fail_at=2), "neo4j:penn", {})
    assert n == 1 and "node O9 is gone" in err


def test_clear_route_refuses_an_unknown_target(settings):
    from .conftest import make_client

    client, _ = make_client(settings)
    with client:
        assert (
            client.get("/api/maps/clear_added", params={"target": "nope"}).status_code
            == 404
        )
        assert (
            client.post("/api/maps/clear_added", json={"target": "nope"}).status_code
            == 404
        )
