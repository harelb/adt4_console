"""The active launch: which experiment runs on which robots, against which prior map.

After a launch that used a prior map, the prior is registered as a ``file:`` graph
source from this machine's copy (paired by run_id) and the Robot viewer opens with that
prior plus ``live:<robot>`` for every launched robot.  The record is kept in a JSON file
(``settings.launch_state_path``) so a console restart mid-run restores both.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import threading
import time

log = logging.getLogger(__name__)


class LaunchState:
    def __init__(self, path: str | None):
        self.path = pathlib.Path(os.path.expanduser(path)) if path else None
        self._lock = threading.Lock()
        self._state: dict | None = None
        if self.path is not None:
            try:
                data = json.loads(self.path.read_text())
                self._state = data if isinstance(data, dict) else None
            except (OSError, ValueError):
                self._state = None

    def get(self) -> dict | None:
        with self._lock:
            return json.loads(json.dumps(self._state)) if self._state else None

    def set(self, state: dict | None) -> None:
        with self._lock:
            self._state = state
            if self.path is None:
                return
            try:
                if state is None:
                    self.path.unlink(missing_ok=True)
                    return
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(state, indent=2))
                tmp.replace(self.path)
            except OSError as e:
                log.warning("could not persist launch state: %s", e)

    def clear(self) -> None:
        self.set(None)


def prior_source_name(local_path: str) -> str:
    """Graph source name (id ``file:<name>``) of a registered prior map."""
    return "prior:" + pathlib.Path(local_path).name


def register_prior(graph, prior: dict) -> str | None:
    """(Re)register ``prior['local_path']`` as a graph source; returns its id."""
    path = prior.get("local_path")
    if not path:
        return None
    try:
        src = graph.add_file(prior_source_name(path), path, label=prior.get("label"))
    except FileNotFoundError as e:
        log.warning("prior map %s: %s", path, e)
        return None
    return src.id


def default_sources(state: dict | None) -> list[str]:
    """Viewer default: the registered prior + the live source of every robot."""
    if not state:
        return []
    out = []
    prior = state.get("prior") or {}
    if prior.get("source_id"):
        out.append(prior["source_id"])
    out += [f"live:{r}" for r in state.get("robots") or []]
    return out


def restore(svc) -> None:
    """Startup: re-register the active launch's prior map (if its copy still exists)."""
    state = svc.launch_state.get()
    prior = (state or {}).get("prior")
    if not prior or not prior.get("local_path"):
        return
    sid = register_prior(svc.graph, prior)
    if sid != prior.get("source_id"):
        prior["source_id"] = sid
        prior["pending"] = sid is None
        state["prior"] = prior
        svc.launch_state.set(state)


def new_state(plan, prior: dict | None) -> dict:
    return {
        "experiment": plan.experiment,
        "robots": list(plan.robots),
        "mode": plan.mode,
        "base_station": plan.base_station,
        "started_at": time.time(),
        "prior": prior,
    }


# Launch form fields remembered between launches (routes/launch.py LaunchPlanBody)
REMEMBERED_FIELDS = (
    "robots",
    "session",
    "experiment",
    "mode",
    "prior_path",
    "prior_source",
    "record",
    "base_station",
    "bs_session",
    "link_network",
    "link_mode",
)
NO_BASE = "-"  # key of launches without a base station


class LaunchMemory:
    """The Launch form's last choices, per base station.

    Saved on every launch request and kept in a JSON file
    (``settings.launch_memory_path``), so a relaunch after Stop & save, a console
    restart or another browser starts from the same prior map, sessions and robots.
    File layout: ``{"by_base": {<base or "-">: {...fields, "saved_at": t}}, "last": <key>}``.
    """

    def __init__(self, path: str | None):
        self.path = pathlib.Path(os.path.expanduser(path)) if path else None
        self._lock = threading.Lock()
        self._data: dict = {"by_base": {}, "last": None}
        if self.path is not None:
            try:
                data = json.loads(self.path.read_text())
                if isinstance(data, dict) and isinstance(data.get("by_base"), dict):
                    self._data = {"by_base": data["by_base"], "last": data.get("last")}
            except (OSError, ValueError):
                pass

    def remember(self, fields: dict) -> dict:
        entry = {k: fields.get(k) for k in REMEMBERED_FIELDS}
        entry["saved_at"] = time.time()
        key = entry.get("base_station") or NO_BASE
        with self._lock:
            self._data["by_base"][key] = entry
            self._data["last"] = key
            self._save()
        return dict(entry)

    def get(self, base_station: str | None = None) -> dict | None:
        """The last launch with ``base_station`` (None: the most recent launch);
        falls back to the most recent one when that base station never launched."""
        with self._lock:
            by = self._data["by_base"]
            entry = by.get(base_station or NO_BASE) if base_station else None
            if entry is None and self._data.get("last"):
                entry = by.get(self._data["last"])
            return json.loads(json.dumps(entry)) if entry else None

    def _save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=2))
            tmp.replace(self.path)
        except OSError as e:
            log.warning("could not persist the launch form: %s", e)


def memory_from_active(state: dict | None) -> dict | None:
    """Launch-form choices recovered from an active-launch record (consoles that
    launched before LaunchMemory existed): robots, mode, base and the prior map."""
    if not state:
        return None
    prior = state.get("prior") or {}
    return {
        "robots": list(state.get("robots") or []),
        "experiment": state.get("experiment"),
        "mode": state.get("mode"),
        "base_station": state.get("base_station"),
        "prior_path": prior.get("path") or "",
        "prior_source": prior.get("source_machine"),
        "saved_at": state.get("started_at"),
    }
