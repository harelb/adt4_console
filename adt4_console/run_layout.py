"""Per-run output layout: where an experiment directory's files are.

``run-adt4 -o <experiment>`` writes each launch to ``<experiment>/runs/<run_id>/`` and
points ``<experiment>/latest`` at it (``dcist_launch_system.fleet.run_dirs``).  Older
experiment directories are *flat* (``hydra/``, ``image_store/`` directly inside).
Anything here that reads a run's files from an experiment path (a Maps entry, a
prior, a ``--map`` source) resolves it first with :func:`resolve_run_dir`.

``fleet.run_dirs`` does the resolution when the workspace has it; the fallback below
(fleet tooling without run_dirs) follows the same rules.
"""

from __future__ import annotations

import os
import pathlib

LATEST = "latest"
RUNS_DIR = "runs"
# a run holds a map when it has one of these (fleet.run_dirs.MAP_MARKERS)
DSG_FILES = (
    "hydra/backend/dsg.json",
    "hydra/backend/dsg_with_mesh.json",
    "backend/dsg.json",
    "dsg.json",
    "dsg_with_mesh.json",
)


def _run_dirs():
    try:
        from dcist_launch_system.fleet import run_dirs
    except ImportError:
        return None
    return run_dirs


def _runs_of(path: str) -> list[str]:
    root = os.path.join(path, RUNS_DIR)
    try:
        dirs = [e.path for e in os.scandir(root) if e.is_dir(follow_symlinks=False)]
    except OSError:
        return []

    def key(p):
        try:
            return (os.stat(p).st_mtime, p)
        except OSError:
            return (0.0, p)

    return sorted(dirs, key=key)


def is_experiment_dir(path) -> bool:
    """``path`` uses the per-run layout (has ``latest`` or ``runs/``)."""
    p = str(path)
    return os.path.lexists(os.path.join(p, LATEST)) or os.path.isdir(
        os.path.join(p, RUNS_DIR)
    )


def resolve_run_dir(path) -> pathlib.Path:
    """The run directory behind ``path``: ``<path>/latest`` resolved, else the newest
    ``<path>/runs/*``, else ``path`` itself (a run dir or a flat legacy dir)."""
    p = os.path.expanduser(str(path))
    rd = _run_dirs()
    if rd is not None and hasattr(rd, "resolve_run_dir"):
        return pathlib.Path(rd.resolve_run_dir(p))
    link = os.path.join(p, LATEST)
    if os.path.isdir(link):
        return pathlib.Path(os.path.realpath(link))
    runs = _runs_of(p)
    return pathlib.Path(runs[-1] if runs else p)


def _dsg_in(run) -> pathlib.Path | None:
    for rel in DSG_FILES:
        f = pathlib.Path(run) / rel
        if f.is_file():
            return f
    return None


def map_candidates(path) -> list[pathlib.Path]:
    """Directories that may hold ``path``'s map, best first: the resolved run
    (``latest``), then older runs newest first (a latest run that crashed before
    saving has no map), then ``path`` itself (flat legacy dir)."""
    p = pathlib.Path(os.path.expanduser(str(path)))
    out = [resolve_run_dir(p)]
    if is_experiment_dir(p):
        out += [pathlib.Path(r) for r in reversed(_runs_of(str(p)))]
    out.append(p)
    seen, uniq = set(), []
    for d in out:
        key = os.path.realpath(d)
        if key not in seen:
            seen.add(key)
            uniq.append(d)
    return uniq


def find_dsg(path) -> tuple[pathlib.Path, pathlib.Path] | None:
    """``(dsg file, run dir)`` of the newest run under ``path`` that has a map."""
    for d in map_candidates(path):
        f = _dsg_in(d)
        if f is not None:
            return f, d
    return None


def experiment_of(run_dir) -> pathlib.Path:
    """The experiment dir a run dir belongs to (``<exp>/runs/<id>`` -> ``<exp>``)."""
    p = pathlib.Path(run_dir)
    return p.parent.parent if p.parent.name == RUNS_DIR else p


def display_name(run_dir) -> str:
    """``<experiment>`` for a per-run dir, else the dir's own name."""
    return experiment_of(run_dir).name


def entry_run_dir(entry: dict) -> str | None:
    """The directory holding a ``run_scan`` entry's files on its machine: its
    ``run_dir`` (per-run layout, run_scan with run_dirs support), else its absolute
    path (flat dir, or fleet tooling whose scan predates per-run dirs)."""
    return entry.get("run_dir") or entry.get("abs_path") or entry.get("path")


def run_subpath(entry: dict) -> str:
    """``runs/<run_id>`` for a per-run entry (relative to the experiment dir), ``""``
    for a flat one: where its files sit inside a copy of the experiment dir."""
    run_dir, top = entry.get("run_dir"), entry.get("abs_path") or entry.get("path")
    if not run_dir or not top:
        return ""
    rel = os.path.relpath(run_dir, top)
    if rel == ".":
        return ""
    if rel.startswith(".."):  # run_dir is a realpath, top is not (a symlinked root)
        head, sep, tail = run_dir.rpartition(f"/{RUNS_DIR}/")
        return f"{RUNS_DIR}/{tail}" if sep and tail and "/" not in tail else ""
    return rel
