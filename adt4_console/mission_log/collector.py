"""Collect a mission into its folder after it ends (plan §5).

One job per mission, run on a thread:

1. **settle**: an automatic collect waits ``settle_s`` after ``mission.end`` so the
   robots' last events (the ``+pad`` of the window) are on disk;
2. **base**: :func:`slicer.run_slice` over the base experiment dir (its runs and
   the console's own ``events/``) into ``base/``;
3. **robots**: for each session robot, unless one is executing a goal (then the
   job waits, ``deferred``, at most ``defer_max_s``): the slicer runs on the
   robot over ssh (its source is sent along, nothing needs installing there) into
   ``<experiment>/missions/<id>/`` there, then rsync pulls that into
   ``<robot>/`` here at ``bwlimit_kbps``.  A robot that cannot be reached stays
   ``pending`` and is retried every ``retry_s`` (and on Collect now);
4. **assemble**: live C7 events into ``<robot>/events.live.jsonl``, ``plans/``,
   the scene-change export, ``mission.json`` run ids, ``manifest.json``,
   ``index.sqlite`` and ``report.json`` (the check).

State per machine (``collect.json``): ``pending`` / ``deferred`` / ``collecting`` /
``collected`` / ``live_only`` / ``failed`` with the last error.
"""

from __future__ import annotations

import base64
import inspect
import json
import logging
import pathlib
import shlex
import shutil
import threading
import time
import zlib

from . import (
    BASE,
    LIVE_FILE,
    PAD_S,
    iter_jsonl,
    read_json,
    scene_export,
    slicer,
    write_json,
)
from . import check as check_mod
from . import index as index_mod
from . import manifest as manifest_mod

log = logging.getLogger(__name__)

SETTLE_S = 60.0
RETRY_S = 300.0
DEFER_MAX_S = 1800.0
DEFER_POLL_S = 10.0
BWLIMIT_KBPS = 2000  # 2 MB/s
SSH_TIMEOUT_S = 120


def slicer_command(args: list[str], python: str = "python3") -> str:
    """A shell command that runs this module's slicer with ``args`` on a machine
    that has only Python (the source travels compressed in the command)."""
    src = pathlib.Path(slicer.__file__).read_bytes()
    blob = base64.b64encode(zlib.compress(src, 9)).decode()
    code = f"import base64,zlib,sys;exec(compile(zlib.decompress(base64.b64decode('{blob}')),'adt4-mission-slice','exec'))"
    return f"{python} -c {shlex.quote(code)} " + " ".join(shlex.quote(a) for a in args)


class Collector:
    def __init__(self, *, ssh=None, rsync=None, endpoint=None, output_root="~/adt4_output", executing=None,
                 run_read=None, live_root=None, clock=time.time, settle_s=SETTLE_S, retry_s=RETRY_S,
                 defer_max_s=DEFER_MAX_S, defer_poll_s=DEFER_POLL_S, bwlimit_kbps=BWLIMIT_KBPS,
                 on_change=None, sleep=None):  # fmt: skip
        self.ssh = ssh
        self.rsync = rsync
        self.endpoint = endpoint  # name -> {user, ip, name}; None -> this machine
        self.output_root = output_root
        self.executing = executing or (lambda: [])
        self.run_read = run_read  # query -> {rows} | {error}; None: no scene export
        self.live_root = live_root  # () -> base event root (events/live/<robot>.jsonl)
        self.clock = clock
        self.settle_s = settle_s
        self.retry_s = retry_s
        self.defer_max_s = defer_max_s
        self.defer_poll_s = defer_poll_s
        self.bwlimit_kbps = bwlimit_kbps
        self.on_change = on_change
        self._stop = threading.Event()
        self._sleep = sleep or (lambda s: self._stop.wait(s))
        self.lock = threading.RLock()
        self.jobs: dict[str, dict] = {}  # mission_id -> {thread, mdir, now: Event}
        self._retry_thread: threading.Thread | None = None

    # ------------------------------------------------------------ status
    def status(self, mdir) -> dict:
        return read_json(pathlib.Path(mdir) / "collect.json") or {
            "state": None,
            "machines": {},
        }

    def _save(self, mdir: pathlib.Path, st: dict) -> None:
        st["t"] = self.clock()
        ms = [v.get("state") for v in st["machines"].values()]
        if any(s in ("collecting", "settling") for s in ms) or st.get("phase") in (
            "settle",
            "slicing",
            "assembling",
        ):
            st["state"] = "running"
        elif any(s == "deferred" for s in ms):
            st["state"] = "deferred"
        elif any(s in ("pending", "failed") for s in ms):
            st["state"] = "pending"
        else:
            st["state"] = "done"
        write_json(mdir / "collect.json", st)
        if self.on_change is not None:
            try:
                self.on_change(mdir.name, st)
            except Exception:  # noqa: BLE001
                log.exception("collect on_change")

    # ------------------------------------------------------------ jobs
    def collect(
        self,
        mdir,
        *,
        auto: bool = False,
        full: bool = False,
        machines=None,
        wait: bool = False,
    ) -> dict:
        """Start (or poke: Collect now) the mission's job."""
        mdir = pathlib.Path(mdir)
        mid = mdir.name
        with self.lock:
            job = self.jobs.get(mid)
            if job and job["thread"].is_alive():
                job["now"].set()  # skip the settle / deferral wait
                return self.status(mdir)
            now = threading.Event()
            if not auto:
                now.set()
            t = threading.Thread(
                target=self._run,
                args=(mdir, now, full, machines),
                name=f"collect-{mid}",
                daemon=True,
            )
            self.jobs[mid] = {"thread": t, "mdir": mdir, "now": now}
            t.start()
        if wait:
            t.join()
        self._ensure_retry()
        return self.status(mdir)

    def stop(self) -> None:
        self._stop.set()
        with self.lock:
            for j in self.jobs.values():
                j["now"].set()

    def _ensure_retry(self) -> None:
        if self._retry_thread is None or not self._retry_thread.is_alive():
            self._retry_thread = threading.Thread(
                target=self._retry_loop, name="collect-retry", daemon=True
            )
            self._retry_thread.start()

    def _retry_loop(self) -> None:
        while not self._stop.wait(self.retry_s):
            with self.lock:
                jobs = [j for j in self.jobs.values() if not j["thread"].is_alive()]
            for j in jobs:
                st = self.status(j["mdir"])
                todo = [
                    m
                    for m, v in (st.get("machines") or {}).items()
                    if v.get("state") in ("pending", "failed")
                ]
                if todo:
                    log.info("collect %s: retrying %s", j["mdir"].name, ", ".join(todo))
                    self.collect(j["mdir"], machines=todo)

    def watch(self, mdirs) -> None:
        """Missions left pending by an earlier console: retry them too."""
        for d in mdirs:
            d = pathlib.Path(d)
            st = self.status(d)
            if (
                st.get("state") in ("pending", "deferred", "running")
                and d.name not in self.jobs
            ):
                self.collect(d, machines=[m for m, v in st.get("machines", {}).items()
                                          if v.get("state") != "collected"] or None)  # fmt: skip

    # ------------------------------------------------------------ the job
    def _run(self, mdir: pathlib.Path, now: threading.Event, full: bool, only) -> None:
        try:
            self._job(mdir, now, full, only)
        except Exception as e:  # noqa: BLE001
            log.exception("collect %s", mdir.name)
            st = self.status(mdir)
            st.setdefault("machines", {})
            st["error"] = f"{type(e).__name__}: {e}"
            st["phase"] = "error"
            self._save(mdir, st)

    def _job(self, mdir: pathlib.Path, now: threading.Event, full: bool, only) -> None:
        mission = read_json(mdir / "mission.json") or {}
        man = manifest_mod
        session = man.session_of(mdir, mission)
        expected = man.expected_machines(mission, session)
        st = self.status(mdir)
        st.setdefault("machines", {})
        st["expected"] = expected
        st["mission_id"] = mdir.name
        for m in expected:
            st["machines"].setdefault(m, {"state": "pending"})
        todo = [m for m in expected if only is None or m in only]
        t_end = float(mission.get("t_end") or self.clock())
        if not now.is_set() and self.clock() - t_end < self.settle_s:
            st["phase"] = "settle"
            self._save(mdir, st)
            now.wait(max(0.0, self.settle_s - (self.clock() - t_end)))
        st["phase"] = "slicing"
        self._save(mdir, st)
        t0 = float(mission.get("t_start") or t_end)
        mid = mission.get("mission_id") or mdir.name
        sid = mission.get("session_id")
        exp = (
            mission.get("experiment")
            or session.get("experiment")
            or mdir.parent.parent.name
        )
        if BASE in todo:
            st["machines"][BASE] = {"state": "collecting", "t": self.clock()}
            self._save(mdir, st)
            try:
                r = slicer.run_slice([str(mdir.parent.parent)], mid, t0, t_end, str(mdir / BASE), session=sid,
                                     pad=PAD_S, root_events=True, machine=BASE)  # fmt: skip
                st["machines"][BASE] = {"state": "collected", "t": self.clock(), "runs": [x["run_id"] for x in r["runs"]],
                                        "n_events": r["n_events"]}  # fmt: skip
            except Exception as e:  # noqa: BLE001
                st["machines"][BASE] = {
                    "state": "failed",
                    "error": f"{type(e).__name__}: {e}",
                    "t": self.clock(),
                }
            self._save(mdir, st)
        offsets = self._offsets(mdir)
        t_defer = self.clock()
        for robot in [m for m in todo if m != BASE]:
            while not now.is_set() and not self._stop.is_set():
                busy = list(self.executing() or [])
                if not busy or self.clock() - t_defer >= self.defer_max_s:
                    break
                st["machines"][robot] = {**st["machines"].get(robot, {}), "state": "deferred",
                                         "why": f"executing: {', '.join(busy)}", "t": self.clock()}  # fmt: skip
                self._save(mdir, st)
                now.wait(self.defer_poll_s)
            st["machines"][robot] = {"state": "collecting", "t": self.clock()}
            self._save(mdir, st)
            st["machines"][robot] = self._collect_robot(
                mdir, robot, exp, mid, sid, t0, t_end, offsets.get(robot, 0.0), full
            )
            self._save(mdir, st)
        st["phase"] = "assembling"
        self._save(mdir, st)
        self.assemble(mdir, mission)
        for m, v in st["machines"].items():
            if (
                v.get("state") in ("pending", "failed")
                and (mdir / m / LIVE_FILE).exists()
            ):
                v["live_only"] = True
        st["phase"] = "done"
        st["t_done"] = self.clock()
        self._save(mdir, st)

    def _offsets(self, mdir: pathlib.Path) -> dict:
        """Robot clock minus base clock at the mission (s): the base's last sample."""
        out = {}
        for rec in iter_jsonl(mdir / BASE / "events" / "console.jsonl"):
            if rec.get("type") == "clock.offset":
                p = rec.get("payload") or {}
                if p.get("robot") and p.get("offset_ms") is not None:
                    out[p["robot"]] = float(p["offset_ms"]) / 1000.0
        return out

    def _collect_robot(self, mdir, robot, exp, mid, sid, t0, t1, offset, full) -> dict:
        t = self.clock()
        if self.ssh is None or self.rsync is None or self.endpoint is None:
            return {
                "state": "pending",
                "error": "no ssh / rsync in this console",
                "t": t,
            }
        try:
            ep = self.endpoint(robot)
            here = self.endpoint(None)
        except Exception as e:  # noqa: BLE001
            return {"state": "pending", "error": f"unknown machine: {e}", "t": t}
        rexp = f"{self.output_root.rstrip('/')}/{exp}"
        rout = f"{rexp}/missions/{mid}"
        args = ["--exp", rexp, "--mission", mid, "--t0", repr(t0), "--t1", repr(t1), "--out", rout,
                "--pad", repr(PAD_S), "--offset", repr(offset), "--machine", robot]  # fmt: skip
        if sid:
            args += ["--session", sid]
        try:
            rc, out, err = self.ssh(
                ep["user"], ep["ip"], slicer_command(args), timeout=SSH_TIMEOUT_S
            )
        except Exception as e:  # noqa: BLE001
            return {"state": "pending", "error": f"ssh: {e}", "t": t}
        if rc != 0:
            return {
                "state": "pending",
                "error": f"slice rc {rc}: {(err or out or '')[-400:]}",
                "t": t,
            }
        try:
            summary = json.loads((out or "").strip().splitlines()[-1])
        except (ValueError, IndexError):
            summary = {}
        dest = mdir / robot
        kw = {}
        try:
            ps = inspect.signature(self.rsync).parameters
            if "bwlimit_kbps" in ps or any(x.kind is inspect.Parameter.VAR_KEYWORD for x in ps.values()):
                kw["bwlimit_kbps"] = self.bwlimit_kbps
        except (TypeError, ValueError):
            pass
        exclude = None if full else ["mission_bag*/", "image_store/", "ros_log/"]
        try:
            ok, method, msg = self.rsync(ep["user"], ep["ip"], rout, here.get("user"), here.get("ip"), str(dest),
                                         exclude=exclude, **kw)  # fmt: skip
        except Exception as e:  # noqa: BLE001
            return {"state": "pending", "error": f"rsync: {e}", "t": t}
        if not ok:
            return {
                "state": "pending",
                "error": f"rsync ({method}): {(msg or '')[-400:]}",
                "t": t,
            }
        if not (dest / "slice.json").exists():
            return {"state": "failed", "error": "no slice.json after the copy", "t": t}
        return {"state": "collected", "t": self.clock(), "runs": summary.get("runs"), "n_events": summary.get("n_events"),
                "bwlimit_kbps": kw.get("bwlimit_kbps"), "remote": rout}  # fmt: skip

    # ------------------------------------------------------------ assemble
    def assemble(self, mdir: pathlib.Path, mission: dict | None = None) -> dict:
        mission = mission or read_json(mdir / "mission.json") or {}
        out = {}
        out["live"] = self._live_copies(mdir, mission)
        out["plans"] = write_plans(mdir)
        if self.run_read is not None:
            try:
                out["scene"] = scene_export.export(self.run_read, float(mission.get("t_start") or 0),
                                                   float(mission.get("t_end") or self.clock()), mdir)  # fmt: skip
            except Exception as e:  # noqa: BLE001
                out["scene"] = {"ok": False, "error": str(e)}
        update_run_ids(mdir)
        manifest_mod.build(mdir)
        index_mod.build(mdir)
        out["report"] = check_mod.run(mdir)["status"]
        return out

    def _live_copies(self, mdir: pathlib.Path, mission: dict) -> dict:
        root = self.live_root() if callable(self.live_root) else self.live_root
        if not root:
            return {}
        live = pathlib.Path(root) / "events" / "live"
        t0, t1 = (
            float(mission.get("t_start") or 0) - PAD_S,
            float(mission.get("t_end") or self.clock()) + PAD_S,
        )
        mid = mission.get("mission_id")
        n = {}
        for p in sorted(live.glob("*.jsonl")) if live.is_dir() else []:
            robot = p.stem
            rows = [
                r
                for r in iter_jsonl(p)
                if t0 <= float(r.get("t_wall") or 0) <= t1 or r.get("mission_id") == mid
            ]
            if not rows:
                continue
            d = mdir / robot
            d.mkdir(parents=True, exist_ok=True)
            with open(d / LIVE_FILE, "w") as f:
                for r in rows:
                    f.write(json.dumps(r, default=str, separators=(",", ":")) + "\n")
            n[robot] = len(rows)
        return n


def write_plans(mdir: pathlib.Path) -> int:
    """``plans/<plan_id>.json`` (request, solved/failed, published) + its PDDL dumps."""
    plans: dict[str, dict] = {}
    req_by_n: dict = {}
    for rec in iter_jsonl(mdir / BASE / "events" / "omniplanner.jsonl"):
        t, p = rec.get("type"), rec.get("payload") or {}
        if t == "plan.request":
            req_by_n[p.get("request")] = rec
        elif t in ("plan.solved", "plan.failed", "plan.published", "plan.superseded"):
            ids = {
                rec.get("plan_id"),
                p.get("plan_id"),
                *((p.get("plan_ids") or {}).values()),
            } - {None, ""}
            for pid in ids:
                d = plans.setdefault(pid, {"plan_id": pid, "events": []})
                d["events"].append(rec)
                if t == "plan.solved":
                    d["solved"] = p
                    if p.get("request") in req_by_n:
                        d["request"] = req_by_n[p["request"]].get("payload")
    if not plans:
        return 0
    out = mdir / "plans"
    out.mkdir(exist_ok=True)
    dumps = {
        p.name: p for p in (mdir / BASE).glob("runs/*/omniplanner/*") if p.is_file()
    }
    for pid, d in plans.items():
        write_json(out / f"{pid}.json", d)
        refs = []
        for e in d["events"]:
            check_mod._refs(e.get("payload"), refs)  # noqa: SLF001
        for ref in refs:
            name = pathlib.Path(str(ref["$file"])).name
            if name in dumps:
                shutil.copy2(dumps[name], out / f"{pid}.{name}")
    return len(plans)


def update_run_ids(mdir: pathlib.Path) -> dict:
    """``mission.json`` ``run_ids``: every machine's runs found by the slices."""
    m = read_json(mdir / "mission.json")
    if not m:
        return {}
    ids = dict(m.get("run_ids") or {})
    for d in sorted(mdir.iterdir()):
        sl = read_json(d / "slice.json") if d.is_dir() else None
        if sl:
            ids[d.name] = [r["run_id"] for r in sl.get("runs") or []]
    if ids != m.get("run_ids"):
        m["run_ids"] = ids
        write_json(mdir / "mission.json", m)
    return ids
