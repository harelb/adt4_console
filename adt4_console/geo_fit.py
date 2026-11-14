"""2-D map -> UTM fits from (map x/y, GPS UTM) pairs.

``umeyama2d`` / ``residuals`` / ``fit`` are the fit ``scripts/geo/anchor_from_gps.py``
uses offline (bag GPS + Hydra trajectory); :func:`fit_anchor` adds the checks a
live estimate needs (too few pairs or too short a baseline: translation only,
heading unknown; too few for that: none).
"""

from __future__ import annotations

import math

import numpy as np

# a rigid (yaw) fit needs this many pairs spread over this much ground
MIN_RIGID_PAIRS = 6
MIN_RIGID_BASELINE_M = 8.0
# below this many pairs there is no estimate at all
MIN_PAIRS = 3


def umeyama2d(src, dst, with_scale=False):
    mu_s, mu_d = src.mean(0), dst.mean(0)
    s, d = src - mu_s, dst - mu_d
    H = s.T @ d / len(src)
    U, S, Vt = np.linalg.svd(H)
    D = np.diag([1, np.sign(np.linalg.det(Vt.T @ U.T))])
    R = Vt.T @ D @ U.T
    sc = (S * np.diag(D)).sum() / (s**2).sum(1).mean() if with_scale else 1.0
    t = mu_d - sc * R @ mu_s
    return R, t, sc


def residuals(R, t, sc, src, dst):
    return np.linalg.norm((sc * (R @ src.T)).T + t - dst, axis=1)


def fit(mp, up, reject=True):
    """Rigid fit of map points ``mp`` (N x >=2) to UTM ``up`` (N x 2) with one
    round of outlier rejection (> 3 x median residual, at least 0.5 m)."""
    R, t, sc = umeyama2d(mp[:, :2], up)
    r = residuals(R, t, sc, mp[:, :2], up)
    keep = np.ones(len(r), bool)
    if reject:
        keep = r <= max(3 * np.median(r), 0.5)
        R, t, sc = umeyama2d(mp[keep, :2], up[keep])
        r = residuals(R, t, sc, mp[keep, :2], up[keep])
    return R, t, r, keep


def baseline(mp) -> float:
    """Spread of the map points: 2 x the standard deviation along their major
    axis (a straight 10 m drive gives ~5.8 m; a single spot gives ~0)."""
    p = np.asarray(mp, float)[:, :2]
    if len(p) < 2:
        return 0.0
    ev = np.linalg.eigvalsh(np.cov((p - p.mean(0)).T))
    return float(2.0 * math.sqrt(max(ev[-1], 0.0)))


def fit_anchor(map_xy, utm_xy, *, min_pairs=MIN_PAIRS, min_rigid_pairs=MIN_RIGID_PAIRS,
               min_baseline_m=MIN_RIGID_BASELINE_M) -> dict | None:  # fmt: skip
    """``utm = R(yaw) @ map + t`` from paired points, or None with fewer than
    ``min_pairs``. With enough pairs over a long enough baseline: a rigid fit
    (``mode: rigid``, ``heading_known: True``). Otherwise translation only with
    yaw 0, i.e. map axes taken as east / north (``mode: translation``,
    ``heading_known: False``), the same assumption ``ADT4_ORIGIN_UTM`` makes."""
    mp = np.asarray(map_xy, float).reshape(-1, 2)
    up = np.asarray(utm_xy, float).reshape(-1, 2)
    if len(mp) != len(up):
        raise ValueError("map and UTM point counts differ")
    ok = np.isfinite(mp).all(1) & np.isfinite(up).all(1)
    mp, up = mp[ok], up[ok]
    n = len(mp)
    if n < min_pairs:
        return None
    base = baseline(mp)
    if n >= min_rigid_pairs and base >= min_baseline_m:
        R, t, r, keep = fit(mp, up)
        yaw = float(math.atan2(R[1, 0], R[0, 0]))
        mode = "rigid"
    else:
        d = up - mp
        t = np.median(d, axis=0)
        r = np.linalg.norm(d - t, axis=1)
        keep = r <= max(3 * np.median(r), 0.5)
        t = d[keep].mean(0)
        r = np.linalg.norm(d[keep] - t, axis=1)
        yaw = 0.0
        mode = "translation"
    return {
        "yaw": yaw,
        "t": [float(t[0]), float(t[1])],
        "rms": float(np.sqrt((r**2).mean())) if len(r) else 0.0,
        "max_residual": float(r.max()) if len(r) else 0.0,
        "n": int(keep.sum()),
        "n_pairs": n,
        "baseline_m": round(base, 2),
        "mode": mode,
        "heading_known": mode == "rigid",
    }
