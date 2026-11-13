"""Console-side grouping / geometry checks of SAM3 dry-run candidates
(adt4_console.sam3_candidates), on the 2026-11-11 field search."""

from __future__ import annotations

from adt4_console import sam3_client
from adt4_console.sam3_candidates import cluster, geometry, view_refs

IMG = "data:image/jpeg;base64,/9j/AAAA"
LABEL = "green trash can"


def _cand(i, score, cen, size, n=400, merge=None, views=2):
    lo = [c - s / 2 for c, s in zip(cen, size)]
    hi = [c + s / 2 for c, s in zip(cen, size)]
    return {
        "index": i,
        "label": LABEL,
        "score": score,
        "centroid": list(cen),
        "bbox_min": lo,
        "bbox_max": hi,
        "num_points": n,
        "num_views": views,
        "num_frames": views,
        "frame_keys": [f"kf{i}-{k}" for k in range(views)],
        "would_merge_into": merge,
        "image_uri": IMG,
        "views": [
            {
                "view": k,
                "frame_key": f"kf{i}-{k}",
                "score": score,
                "bbox_2d": [0, 0, 10, 10],
                "image_uri": IMG if k == 0 else None,
            }
            for k in range(views)
        ],
    }


#: the 8 candidates the operator saw (server shape; sizes as printed, a printed
#: 0.0 is a few mm)
FIELD = {
    "pending_id": "p-field",
    "prompt": LABEL,
    "expires_in_s": 120.0,
    "candidates": [
        _cand(0, 0.94, (-47.3, -12.6, -0.7), (0.01, 0.2, 0.7)),
        _cand(1, 0.93, (-0.8, 2.2, -0.2), (0.2, 0.1, 0.7)),
        _cand(2, 0.92, (-1.1, 1.8, -1.2), (0.5, 0.1, 0.7)),
        _cand(3, 0.82, (-47.0, 3.3, -0.6), (0.4, 0.1, 0.7)),
        _cand(4, 0.81, (-0.6, 2.3, -0.0), (0.1, 0.01, 0.4)),
        _cand(5, 0.47, (-19.8, -2.6, 0.1), (0.4, 0.3, 0.6)),
        _cand(6, 0.35, (-1.1, 1.6, 0.5), (0.01, 0.1, 0.1)),
        _cand(7, 0.29, (-46.1, 3.7, -0.2), (0.1, 0.3, 0.7)),
    ],
}


def test_geometry_tiers():
    assert geometry(_cand(0, 0.9, (0, 0, 0), (0.2, 0.1, 0.7))) == (0, [])
    assert geometry(_cand(0, 0.9, (0, 0, 0), (0.01, 0.2, 0.7)))[0] == 1  # flat
    assert geometry(_cand(0, 0.9, (0, 0, 0), (0.01, 0.1, 0.1)))[0] == 2  # sliver
    assert geometry(_cand(0, 0.9, (0, 0, 0), (0.05, 0.05, 0.05)))[0] == 2  # speck
    assert geometry(_cand(0, 0.9, (0, 0, 0), (0.01, 0.01, 0.7)))[0] == 2  # line
    tier, flags = geometry(_cand(0, 0.9, (0, 0, 0), (0.3, 0.3, 0.7), n=12))
    assert tier == 2 and "low depth support (12 pts)" in flags
    assert geometry({"label": LABEL, "centroid": None})[0] == 2
    assert geometry({"label": LABEL, "centroid": [0, float("nan"), 0]})[0] == 2


def test_field_search_groups_the_same_can_into_one_card():
    ks = cluster(FIELD["candidates"])
    groups = {k["rep"]["index"]: sorted(m["index"] for m in k["members"]) for k in ks}
    # 2, 3, 5, 7 (1-based) are one can: one card, the 0.93 detection represents it
    # (the 0.92 one is 1 m below the others: never the representative)
    assert groups[1] == [1, 2, 4, 6]
    # the cans near (-47, 3..4) are ~1 m apart: two objects (bins side by side)
    assert groups[3] == [3] and groups[7] == [7]
    assert groups[5] == [5]
    assert groups[0] == [0]  # 16 m from the others
    assert len(ks) == 5
    can = next(k for k in ks if k["rep"]["index"] == 1)
    assert can["score"] == 0.93 and can["tier"] == "ok"
    # good geometry first, then score; the flat 0.94 one after the good ones
    assert [k["rep"]["index"] for k in ks] == [1, 3, 5, 7, 0]
    assert ks[-1]["tier"] == "suspect"


def test_z_outlier_and_slivers_never_represent():
    ks = cluster(FIELD["candidates"])
    can = next(k for k in ks if 2 in [m["index"] for m in k["members"]])
    assert can["rep"]["index"] == 1
    assert can["members"][0]["index"] == 1


def test_different_merge_targets_stay_apart():
    a = _cand(
        0, 0.9, (0, 0, 0), (0.3, 0.3, 0.7), merge={"symbol": "O1", "distance_m": 0.1}
    )
    b = _cand(
        1, 0.8, (0.4, 0, 0), (0.3, 0.3, 0.7), merge={"symbol": "O2", "distance_m": 0.1}
    )
    c = _cand(
        2,
        0.7,
        (0.2, 0.1, 0),
        (0.3, 0.3, 0.7),
        merge={"symbol": "O1", "distance_m": 0.3},
    )
    groups = sorted(
        sorted(m["index"] for m in k["members"]) for k in cluster([a, b, c])
    )
    assert groups == [[0, 2], [1]]


def test_different_labels_and_large_footprints():
    a = _cand(0, 0.9, (0, 0, 0), (0.3, 0.3, 0.7))
    b = {**_cand(1, 0.8, (0.1, 0, 0), (0.3, 0.3, 0.7)), "label": "chair"}
    assert len(cluster([a, b])) == 2
    # a 3 m couch seen from both ends: centroids 1.2 m apart, footprints overlap
    c1 = _cand(0, 0.9, (0, 0, 0.4), (1.8, 0.9, 0.8))
    c2 = _cand(1, 0.8, (1.2, 0, 0.4), (1.8, 0.9, 0.8))
    assert len(cluster([c1, c2])) == 1


def test_view_refs_interleave_members_best_first():
    ks = cluster(FIELD["candidates"])
    can = next(k for k in ks if k["rep"]["index"] == 1)
    refs = view_refs(can["members"], 6)
    assert [(r["index"], r["view"]) for r in refs] == [
        (1, 0), (2, 0), (4, 0), (6, 0), (1, 1), (2, 1)
    ]  # fmt: skip
    assert refs[0]["image_uri"] == IMG and refs[4]["image_uri"] is None
    # no view metadata: view 0 of the candidate, its own image
    bare = {"index": 3, "image_uri": IMG}
    assert view_refs([bare]) == [{"index": 3, "view": 0, "image_uri": IMG}]


def test_candidate_events_one_card_per_object():
    ev = sam3_client.candidate_events(FIELD, "neo4j:x")
    assert len(ev) == 5
    can = ev[0]
    assert can["index"] == 1 and can["members"] == [1, 2, 4, 6]
    assert can["score"] == 0.93 and can["geometry"] == "ok" and can["flags"] == []
    assert can["centroid"] == [-0.8, 2.2, -0.2]  # the committed member's geometry
    assert can["num_views"] == 6 and len(can["views"]) == 6
    assert can["views"][1] == {"index": 2, "view": 0, "image_uri": IMG}
    flat = ev[-1]
    assert flat["index"] == 0 and flat["geometry"] == "suspect"
    assert flat["flags"] == ["flat (low depth support)"]
    assert flat["score"] == 0.94  # unchanged


def test_candidates_for_model_matches_the_cards():
    out = sam3_client.candidates_for_model(FIELD)
    assert out["num_candidates"] == 5
    assert [c["index"] for c in out["candidates"]] == [1, 3, 5, 7, 0]
    assert out["candidates"][0]["detections_merged"] == 4
    assert out["candidates"][-1]["flags"] == ["flat (low depth support)"]


def test_old_single_candidate_shape_still_works():
    res = {
        "pending_id": "p",
        "candidates": [
            {"index": 0, "label": "x", "score": 0.5, "centroid": [1, 2, 3]},
        ],
    }
    ev = sam3_client.candidate_events(res, None)
    assert len(ev) == 1 and ev[0]["index"] == 0 and ev[0]["members"] == [0]
    assert ev[0]["views"] == [{"index": 0, "view": 0, "image_uri": None}]
