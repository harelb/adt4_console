"""Console-side clean-up of SAM3 dry-run candidates before the operator sees them.

The query server (openset_query ``build_candidates``) returns one candidate per
discovered object, but its cross-frame association often leaves the same
physical object as several candidates (one per keyframe group), and a mask with
little valid depth back-projects to degenerate geometry (a 0 m extent, a
centroid far off the object). Field run 2026-11-11, "green trash can": 8
candidates, 4 of them the same can within 0.7 m horizontally, one 1 m below the
others, three with a ~0 m dimension.

Here, from the fields the server already sends (centroid, bbox, num_points,
views):

1. every candidate gets a geometry tier (``ok`` / ``suspect`` / ``degenerate``)
   and human-readable ``flags``;
2. candidates of the same label close together are grouped into one cluster
   (one card): its representative is the best-geometry, best-scoring member
   (the index that is committed, so the card shows exactly the geometry that is
   written), its score the best member score, its views every member's views;
3. clusters are ordered good geometry first, then by score (scores unchanged).

TODO(openset_query): association + geometry checks belong in the server
(``association.py`` / ``reprojection.points_to_object``), which has the points:
it can require a minimum depth support per view, take the median over views and
re-fuse duplicates into one object (one commit writes all their observations).
"""

from __future__ import annotations

import math
from statistics import median

#: horizontal centroid distance under which two same-label candidates are the
#: same object. Equals the server's commit merge radius
#: (``Sam3FrontendConfig.dedup_centroid_dist_m``): two candidates closer than this
#: would have merged into one map object anyway when both were approved.
CLUSTER_XY_M = 0.75
#: vertical centroid gap still allowed inside a cluster. Generous because a
#: low-depth-support view puts the centroid far off in z (field run: 1 m).
CLUSTER_Z_M = 1.5
#: footprint (xy bbox) overlap, with this margin, also joins a cluster (large objects)
OVERLAP_MARGIN_M = 0.1

#: an extent below this is "no depth spread" (a flat/one-pixel-row back-projection)
THIN_M = 0.03
#: largest extent below this: a speck, not an object
SPECK_M = 0.08
#: flat (one extent < THIN_M) and also this small: a sliver of depth, not an object
SMALL_FLAT_M = 0.15
#: fewer back-projected points than this: too little depth support to trust
MIN_POINTS = 30
#: a member whose centroid z is this far from its cluster's median z is off-object
Z_OUTLIER_M = 0.5

TIERS = ("ok", "suspect", "degenerate")


def _vec(v) -> list[float] | None:
    try:
        out = [float(x) for x in v]
    except (TypeError, ValueError):
        return None
    if len(out) != 3 or not all(math.isfinite(x) for x in out):
        return None
    return out


def geometry(c: dict) -> tuple[int, list[str]]:
    """(tier index into TIERS, flags) of one raw server candidate."""
    cen = _vec(c.get("centroid"))
    lo, hi = _vec(c.get("bbox_min")), _vec(c.get("bbox_max"))
    if cen is None:
        return 2, ["no 3D position"]
    flags: list[str] = []
    tier = 0
    n = c.get("num_points")
    if isinstance(n, (int, float)) and n < MIN_POINTS:
        tier = 2
        flags.append(f"low depth support ({int(n)} pts)")
    if lo is not None and hi is not None:
        ext = [abs(b - a) for a, b in zip(lo, hi)]
        thin = sum(e < THIN_M for e in ext)
        if max(ext) < SPECK_M:
            tier = 2
            flags.append("tiny extent")
        elif thin >= 2:
            tier = 2
            flags.append("degenerate extent")
        elif thin == 1 and max(ext) < SMALL_FLAT_M:
            tier = 2
            flags.append("degenerate extent")
        elif thin == 1:
            tier = max(tier, 1)
            flags.append("flat (low depth support)")
    return tier, flags


def _xy_dist(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _footprints_overlap(a: dict, b: dict, margin: float = OVERLAP_MARGIN_M) -> bool:
    alo, ahi = _vec(a.get("bbox_min")), _vec(a.get("bbox_max"))
    blo, bhi = _vec(b.get("bbox_min")), _vec(b.get("bbox_max"))
    if None in (alo, ahi, blo, bhi):
        return False
    return all(alo[k] - margin <= bhi[k] and blo[k] - margin <= ahi[k] for k in (0, 1))


def _merge_sym(c: dict) -> str | None:
    m = c.get("would_merge_into")
    return str(m["symbol"]) if isinstance(m, dict) and m.get("symbol") else None


def _same_object(seed: dict, c: dict) -> bool:
    if (seed.get("label") or "") != (c.get("label") or ""):
        return False
    s1, s2 = _merge_sym(seed), _merge_sym(c)
    if s1 and s2 and s1 != s2:  # would merge into two different mapped objects
        return False
    a, b = _vec(seed.get("centroid")), _vec(c.get("centroid"))
    if a is None or b is None or abs(a[2] - b[2]) > CLUSTER_Z_M:
        return False
    return _xy_dist(a, b) <= CLUSTER_XY_M or _footprints_overlap(seed, c)


def _score(c: dict) -> float:
    try:
        return float(c.get("score") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def cluster(cands: list[dict]) -> list[dict]:
    """Group raw server candidates (``index``, ``label``, ``score``,
    ``centroid``, ``bbox_*``, ``num_points``, ``views``) into clusters.

    Greedy from the best candidate (good geometry first, then score): each
    candidate joins the first cluster whose seed is the same object (same label,
    xy centroid within CLUSTER_XY_M or overlapping footprints, z within
    CLUSTER_Z_M, not two different merge targets), else seeds a new cluster. Seed
    only, not any member, so a chain of noisy detections cannot glue two real
    objects together.

    Returns ``[{"rep": candidate, "members": [candidates, rep first], "tier",
    "flags", "score"}]``, ordered good geometry first, then best score.
    """
    rated = []
    for i, c in enumerate(cands):
        c = dict(c)
        c.setdefault("index", i)
        tier, flags = geometry(c)
        rated.append((tier, flags, c))
    rated.sort(key=lambda t: (t[0], -_score(t[2])))
    groups: list[list[tuple[int, list[str], dict]]] = []
    for t in rated:
        for g in groups:
            if _same_object(g[0][2], t[2]):
                g.append(t)
                break
        else:
            groups.append([t])
    out = []
    for g in groups:
        # z outliers (off-object depth) cannot represent the cluster
        if len(g) >= 3:
            zs = [_vec(m[2]["centroid"])[2] for m in g]
            mz = median(zs)
            g = [
                (max(m[0], 1), m[1] + ["z off the other views"], m[2])
                if abs(z - mz) > Z_OUTLIER_M
                else m
                for m, z in zip(g, zs)
            ]
        g.sort(key=lambda m: (m[0], -_score(m[2])))
        rep_tier, rep_flags, rep = g[0]
        out.append(
            {
                "rep": rep,
                "members": [m[2] for m in g],
                "tier": TIERS[rep_tier],
                "flags": rep_flags,
                "score": max(_score(m[2]) for m in g),
            }
        )
    out.sort(key=lambda k: (TIERS.index(k["tier"]), -k["score"]))
    return out


def view_refs(members: list[dict], limit: int = 6) -> list[dict]:
    """The views a cluster card can show, as ``{"index", "view", "image_uri"}``
    (a view is fetched by the member's index + its view number): every member's
    best view first, then their second views, ...; at most ``limit``."""
    per = []
    for m in members:
        vs = [v for v in (m.get("views") or []) if isinstance(v, dict)]
        n = len(vs) or 1  # a candidate without view metadata still has view 0
        per.append(
            [
                {
                    "index": int(m.get("index", 0)),
                    "view": int(vs[k].get("view", k)) if vs else 0,
                    "image_uri": (vs[k].get("image_uri") if vs else None)
                    or (m.get("image_uri") if k == 0 else None),
                }
                for k in range(n)
            ]
        )
    out: list[dict] = []
    depth = 0
    while len(out) < limit and any(depth < len(p) for p in per):
        for p in per:
            if depth < len(p) and len(out) < limit:
                out.append(p[depth])
        depth += 1
    return out
