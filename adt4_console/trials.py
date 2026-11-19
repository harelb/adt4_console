"""Trials: one operator-started recording across the fleet (API.md §23).

*Start recording* makes a trial id, tells every chosen machine to record into
``<output_root>/<trial_id>/<machine>/`` (``adt4-rec``, the helper in
Awesome-DCIST-T4's ``dcist_launch_system/bin``) and then keeps checking that
each one still is.  *Stop* closes every bag.  In between the operator adds
*flags* (timestamped notes).

What goes where:

* each machine: its bag directory, with its own ``trial.json``, the recorder's
  log and (at stop) the stack's tmux panes.  Written by ``adt4-rec``; nothing
  recorded crosses the radio.
* this machine: ``<output_root>/<trial_id>/console/``
  - ``trial.json``    the trial: description, machines, what each start, check
    and stop returned, clock offsets
  - ``events.jsonl``  one JSON object per line, appended and fsynced as it
    happens: start / stop, every flag, every state change of a machine, every
    operator action sent to the console (see ``TrialRequestLog``)

Rules (keep them when editing):

* The only traffic is ssh: one command per machine at start and at stop, one
  small ``status`` per machine every ``trial_poll_s`` while recording.
* A machine that cannot be reached is *unknown*, never assumed recording: its
  state says so and carries the age of the last answer.
* Nothing here stops a recording by itself.  Low disk is a warning.
* A failure in this module must not break the console: the poll loop and the
  request log swallow and log their own errors.
"""

from __future__ import annotations

import asyncio
import copy
import datetime
import json
import logging
import os
import pathlib
import queue
import re
import shlex
import subprocess
import threading
import time

log = logging.getLogger(__name__)

SCHEMA = 1
HUB_TOPIC = "trial/state"
CONSOLE_DIR = "console"
TRIAL_FILE = "trial.json"
EVENTS_FILE = "events.jsonl"
RATES_FILE = ".trial_rates.json"

DEFAULT_RECORDER = "~/dcist_ws/src/awesome_dcist_t4/dcist_launch_system/bin/adt4-rec"
TOPIC_SETS = {"spot": "spot", "phoenix": "phoenix"}  # platform_type -> topic list
BASE_TOPIC_SET = "base_station"

START_TIMEOUT_S = 45.0
STATUS_TIMEOUT_S = 8.0
STOP_TIMEOUT_S = 90.0
CHECK_TIMEOUT_S = 30.0
CHECK_DELAY_S = 5.0
CHECK_TRIES = 6  # the topic check is repeated until a machine answers

# warnings, never actions
LOW_DISK_BYTES = 10 * 10**9
LOW_MINUTES = 10.0
STALL_S = 45.0  # a bag that grew before and has not for this long
# ... but only a bag that writes this much in STALL_S on average: the recorder
# writes a chunk at a time, so a bag of a few small topics (the base station's)
# goes minutes between writes while recording everything
STALL_MIN_BYTES = 8 * 10**6

# machine states
STARTING, RECORDING, FAILED, UNREACHABLE = (
    "starting",
    "recording",
    "failed",
    "unreachable",
)
LOST, STOPPING, STOPPED, STOP_PENDING = "lost", "stopping", "stopped", "stop_pending"
STOP_FAILED, SKIPPED = "stop_failed", "skipped"
# a machine that is (or may be) still recording
LIVE = (RECORDING, UNREACHABLE, STOP_PENDING, STOP_FAILED, STOPPING)

MAX_DESCRIPTION = 500
MAX_FLAG = 2000
TRIAL_RE = re.compile(r"^trial_[A-Za-z0-9][A-Za-z0-9._+-]{0,110}$")


class TrialError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


# --------------------------------------------------------------------- pure
def slug(text: str, limit: int = 40) -> str:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    out = ""
    for w in words:
        if len(out) + len(w) + 1 > limit:
            break
        out = f"{out}-{w}" if out else w
    return out or "trial"


def make_trial_id(description: str, now: float) -> str:
    stamp = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ"
    )
    return f"trial_{stamp}_{slug(description)}"


def iso(t: float) -> str:
    return (
        datetime.datetime.fromtimestamp(t, datetime.timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def topic_set(m: dict) -> str | None:
    """The topic list a machine records: by platform for a robot, the base
    station list otherwise; None when a robot's platform has none."""
    if m.get("role") != "robot":
        return BASE_TOPIC_SET
    return TOPIC_SETS.get(m.get("platform_type") or "spot")


def recorder_command(recorder: str, output_root: str, *args) -> str:
    """``python3 <adt4-rec> --output-root ROOT ARGS`` for a remote shell.
    ``~`` in the two paths is left for that shell to expand."""

    def path(p: str) -> str:
        return "~/" + shlex.quote(p[2:]) if p.startswith("~/") else shlex.quote(p)

    return " ".join(
        ["python3", path(recorder), "--output-root", path(output_root)]
        + [shlex.quote(str(a)) for a in args]
    )


def parse_reply(rc: int, out: str, err: str = "") -> dict:
    """The helper's one JSON line.  Without one: ``{"ok": False, "error",
    "unreachable" | "missing"}`` (ssh failed / the helper is not on the machine)."""
    for line in reversed((out or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if isinstance(data, dict):
                data.setdefault("ok", rc == 0)
                return data
    text = ((err or "") + " " + (out or "")).strip()
    if "No such file" in text or "can't open file" in text or rc == 127:
        return {
            "ok": False,
            "missing": True,
            "error": "adt4-rec is not on this machine (deploy Awesome-DCIST-T4 with the trial recorder)",
        }
    if rc in (-1, 255, 124) or "timeout" in text.lower() or "timed out" in text.lower():
        return {
            "ok": False,
            "unreachable": True,
            "error": f"no answer over ssh ({text[-200:] or rc})",
        }
    return {"ok": False, "error": f"adt4-rec failed (rc {rc}): {text[-300:]}"}


def estimate(free_bytes, rate_bps) -> float | None:
    """Seconds of recording left on a disk at ``rate_bps``."""
    if not free_bytes or not rate_bps or rate_bps <= 0:
        return None
    return free_bytes / rate_bps


def summarize_body(raw: bytes, limit: int = 4000):
    """A request body for the event log: JSON with bulky / secret keys dropped,
    cut to ``limit`` characters; anything else is only counted."""
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return {"_bytes": len(raw)}
    if isinstance(data, dict):
        data = {k: v for k, v in data.items() if k not in DROP_KEYS}
    text = json.dumps(data, sort_keys=True)
    if len(text) <= limit:
        return data
    return {"_truncated": text[:limit], "_chars": len(text)}


DROP_KEYS = {"history", "image", "images", "token", "password", "api_key"}
LOG_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# not operator decisions, or far too many of them
SKIP_PATH_PARTS = ("/teleop/cmd_vel", "/api/trials")
# requests whose answer is part of what happened (the plan a mission got, what
# an edit changed, what the chat said): body and response are kept up to BIG
CAPTURE_PREFIXES = (
    "/api/chat",
    "/api/mission",
    "/api/plan",
    "/api/edit",
    "/api/manipulation",
)
CAPTURE_PARTS = ("/goal/", "/executor/", "/path/")
BIG = 200_000
# console state published to the browsers that is logged as it changes
# screen recordings of the console page, uploaded by the browser in pieces
SCREEN_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
SCREEN_EXT = {"video/webm": "webm", "video/mp4": "mp4", "video/x-matroska": "mkv"}
SCREEN_MAX_CHUNK = 64 * 10**6
SCREEN_LATE_S = 120.0  # the last pieces arrive after the trial has ended
HUB_PREFIXES = ("manipulation/", "mission/", "maps/active", "goal_track/")
HUB_MIN_GAP_S = 0.5
HUB_MAX_CHARS = 20_000
UI_KEYS = ("kind", "label", "page", "detail", "t_client")
UI_MAX_EVENTS = 200


JOB_RE = re.compile(r"^/api/jobs/([^/]+)/(confirm|cancel)$")


def request_category(path: str) -> str:
    """What kind of operator request a path is, so the ones that change
    nothing are told apart in the log: ``preview`` (show what this would do),
    ``dry_run`` (list what a launch would run), ``job_confirm`` (the answer to
    a question a background job asked), ``job_cancel``, else ``action``."""
    m = JOB_RE.match(path)
    if m:
        return "job_confirm" if m.group(2) == "confirm" else "job_cancel"
    if "/dry_run" in path:
        return "dry_run"
    if "preview" in path.rsplit("/", 1)[-1] or "/preview" in path:
        return "preview"
    return "action"


def wants_response(path: str) -> bool:
    return path.startswith(CAPTURE_PREFIXES) or any(p in path for p in CAPTURE_PARTS)


def clip_json(data, limit: int):
    """``data`` if its JSON is at most ``limit`` characters, else a cut copy."""
    try:
        text = json.dumps(data, sort_keys=True, default=str)
    except (TypeError, ValueError):
        text = str(data)
    return (
        data
        if len(text) <= limit
        else {"_truncated": text[:limit], "_chars": len(text)}
    )


MAX_BODY = BIG
MAX_RESPONSE = 200_000


# ------------------------------------------------------------------ service
class TrialService:
    """``helpers``: a callable returning fleet_helpers (``ssh_cmd``,
    ``run_parallel``); ``run_local``: (cmd, timeout) -> (rc, out, err)."""

    def __init__(self, settings, fleet, helpers, hub=None, *, clock=time.time,
                 sleep=time.sleep, run_local=None, background=True):  # fmt: skip
        self.settings = settings
        self.fleet = fleet
        self.helpers = helpers
        self.hub = hub
        self.clock = clock
        self.sleep = sleep
        self.background = background  # False: tests drive start / poll / stop inline
        self._run_local = run_local or _run_local
        self.lock = threading.RLock()
        self._file_lock = threading.Lock()
        self.active: dict | None = None
        self.last: dict | None = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # () -> dict: what the console is working with (launch, map, missions),
        # saved into the trial at start and at stop
        self.context = None
        self._hub_q: queue.Queue = queue.Queue(maxsize=2000)
        self._hub_last: dict[str, tuple[float, int]] = {}
        self._hub_thread: threading.Thread | None = None
        if hub is not None:
            hub.tap = self.hub_tap

    # ------------------------------------------------------------ plumbing
    @property
    def root(self) -> pathlib.Path:
        return pathlib.Path(os.path.expanduser(self.settings.output_root))

    def _console_dir(self, trial_id: str) -> pathlib.Path:
        return self.root / trial_id / CONSOLE_DIR

    def _recorder(self) -> str:
        return getattr(self.settings, "trial_recorder", None) or DEFAULT_RECORDER

    def _poll_s(self) -> float:
        return float(getattr(self.settings, "trial_poll_s", 10.0) or 10.0)

    def _rec(self, m: dict, args: list, timeout: float) -> tuple[dict, float, float]:
        """Run the helper on ``m``: (reply, t_sent, t_received)."""
        cmd = recorder_command(self._recorder(), self.settings.output_root, *args)
        t0 = self.clock()
        try:
            if m.get("local"):
                rc, out, err = self._run_local(cmd, timeout)
            else:
                # ssh's ConnectTimeout takes whole seconds only
                rc, out, err = self.helpers().ssh_cmd(
                    m["user"], m["ip"], cmd, timeout=int(round(timeout))
                )
        except Exception as e:  # noqa: BLE001
            rc, out, err = -1, "", f"{type(e).__name__}: {e}"
        return parse_reply(rc, out, err), t0, self.clock()

    def _parallel(self, fn, items: list) -> dict:
        if not items:
            return {}
        if len(items) == 1:
            return {items[0]["name"]: _guard(fn, items[0])}
        res = self.helpers().run_parallel(lambda m: _guard(fn, m), items)
        return {m["name"]: r for m, r in res}

    def _publish(self) -> None:
        if self.hub is None:
            return
        try:
            self.hub.publish(HUB_TOPIC, self.payload())
        except Exception:  # noqa: BLE001
            log.exception("publishing the trial state")

    # ------------------------------------------------------------- files
    def event(self, kind: str, trial: dict | None = None, **fields) -> dict | None:
        """Append one line to the trial's events.jsonl (fsynced)."""
        trial = trial or self.active
        if trial is None:
            return None
        t = self.clock()
        ev = {"t": round(t, 3), "iso": iso(t), "type": kind, **fields}
        path = self._console_dir(trial["trial_id"]) / EVENTS_FILE
        try:
            with self._file_lock, open(path, "a") as f:
                f.write(json.dumps(ev, sort_keys=True, default=str) + "\n")
                f.flush()
                os.fsync(f.fileno())
        except OSError as e:
            log.error("trial event not written to %s: %s", path, e)
            with self.lock:
                trial["log_error"] = f"events.jsonl: {e}"
            return None
        with self.lock:
            trial["events"] = trial.get("events", 0) + 1
            if kind == "flag":
                trial["flags"] = trial.get("flags", 0) + 1
        return ev

    def _save(self, trial: dict) -> None:
        path = self._console_dir(trial["trial_id"]) / TRIAL_FILE
        with self.lock:
            data = copy.deepcopy(trial)
        tmp = path.with_suffix(".json.tmp")
        try:
            with self._file_lock:
                with open(tmp, "w") as f:
                    json.dump(data, f, indent=2, sort_keys=True, default=str)
                    f.write("\n")
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, path)
        except OSError as e:
            log.error("trial manifest not written to %s: %s", path, e)
            with self.lock:
                trial["log_error"] = f"trial.json: {e}"

    def events_tail(self, n: int = 50, trial_id: str | None = None) -> list[dict]:
        with self.lock:
            trial = self.active or self.last
            tid = trial_id or (trial["trial_id"] if trial else None)
        if not tid or not TRIAL_RE.match(tid):
            return []
        try:
            lines = (self._console_dir(tid) / EVENTS_FILE).read_text().splitlines()
        except OSError:
            return []
        out = []
        for line in lines[-max(1, min(n, 1000)) :]:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
        return out

    def _rates(self) -> dict:
        try:
            data = json.loads((self.root / RATES_FILE).read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _remember_rate(self, name: str, rate_bps: float) -> None:
        rates = self._rates()
        rates[name] = {"rate_bps": round(rate_bps, 1), "t": round(self.clock(), 1)}
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            (self.root / RATES_FILE).write_text(
                json.dumps(rates, indent=2, sort_keys=True) + "\n"
            )
        except OSError as e:
            log.warning("could not save %s: %s", RATES_FILE, e)

    # ------------------------------------------------------------ payload
    def payload(self) -> dict:
        with self.lock:
            return {
                "now": round(self.clock(), 3),
                "poll_s": self._poll_s(),
                "active": copy.deepcopy(self.active),
                "last": copy.deepcopy(self.last),
            }

    def _machines(self, names: list[str] | None) -> list[dict]:
        with self.fleet.lock:
            machines = [dict(m) for m in self.fleet.runtime_config.values()]
        if names is not None:
            known = {m["name"] for m in machines}
            unknown = [n for n in names if n not in known]
            if unknown:
                raise TrialError(404, f"unknown machine(s): {', '.join(unknown)}")
            return [m for m in machines if m["name"] in names]
        # default: every robot that answers, and this machine
        return [
            m for m in machines
            if (m.get("role") == "robot" and m.get("online")) or m.get("local")
        ]  # fmt: skip

    def _robots(self) -> list[str]:
        with self.fleet.lock:
            return sorted(
                n
                for n, m in self.fleet.runtime_config.items()
                if m.get("role") == "robot"
            )

    def _context(self) -> dict | None:
        if self.context is None:
            return None
        try:
            return self.context()
        except Exception as e:  # noqa: BLE001
            log.exception("trial context")
            return {"error": f"{type(e).__name__}: {e}"}

    def _snapshot(self, trial: dict, when: str) -> None:
        """Save what the console is working with into the trial and the log."""
        ctx = self._context()
        if ctx is None:
            return
        with self.lock:
            trial[f"context_{when}"] = ctx
        self.event("context", trial, when=when, context=clip_json(ctx, BIG))

    def _by_name(self, names: list[str]) -> list[dict]:
        """The fleet's machines among ``names`` (one that left the topology
        is skipped: it stays in its last state)."""
        with self.fleet.lock:
            return [dict(m) for n, m in self.fleet.runtime_config.items() if n in names]

    # ---------------------------------------------------------- preflight
    def preflight(self) -> dict:
        """Before a start: per machine, is the helper there, is something
        recording already, free disk and how long it lasts at the rate of that
        machine's last trial."""
        machines = self._machines(None)
        rates = self._rates()

        def one(m):
            reply, _, _ = self._rec(m, ["status"], STATUS_TIMEOUT_S)
            return reply

        replies = self._parallel(one, machines)
        out = []
        for m in sorted(machines, key=lambda x: x["name"]):
            r = replies.get(m["name"]) or {"ok": False, "error": "no reply"}
            rate = (rates.get(m["name"]) or {}).get("rate_bps")
            secs = estimate(r.get("free_bytes"), rate)
            notes = []
            if r.get("recording"):
                notes.append(f"already recording {r.get('trial') or r.get('dir')}")
            if r.get("other_recorders"):
                notes.append(
                    f"{len(r['other_recorders'])} other recorder(s) running (the stack's recording window?)"
                )
            if r.get("ok") and not r.get("stack_running") and m.get("role") == "robot":
                notes.append("the adt4_system session is not running")
            if topic_set(m) is None:
                notes.append(f"no topic list for platform {m.get('platform_type')!r}")
            out.append(
                {
                    "name": m["name"],
                    "role": m.get("role"),
                    "local": bool(m.get("local")),  # this machine: the base station
                    "ready": bool(r.get("ok"))
                    and not r.get("recording")
                    and topic_set(m) is not None,
                    # a recorder is running here already (a trial this console
                    # does not have): a start may stop it and record anew
                    "recording": (r.get("trial") or r.get("dir") or True)
                    if r.get("recording")
                    else None,
                    "replaceable": bool(r.get("ok"))
                    and bool(r.get("recording"))
                    and topic_set(m) is not None,
                    "error": None if r.get("ok") else r.get("error"),
                    "free_bytes": r.get("free_bytes"),
                    "total_bytes": r.get("total_bytes"),
                    "rate_bps": rate,
                    "minutes": None if secs is None else round(secs / 60, 1),
                    "notes": notes,
                }
            )
        base = next((m["name"] for m in machines if m.get("local")), None)
        return {
            "updated": round(self.clock(), 3),
            "machines": out,
            "base_station": base,
            "base_note": None
            if base
            else "this console's machine is not in the topology: no base-station bag will be recorded",
        }

    # -------------------------------------------------------------- start
    def start(
        self,
        description: str,
        machines: list[str] | None = None,
        replace: list[str] | None = None,
    ) -> dict:
        """``replace``: machines whose running recorder (another trial's, or
        one no console knows about) is stopped first, then started for this
        trial.  Without it such a machine is ``failed``: already recording."""
        description = (description or "").strip()
        if not description:
            raise TrialError(400, "a short mission description is required")
        if len(description) > MAX_DESCRIPTION:
            raise TrialError(
                400, f"description is longer than {MAX_DESCRIPTION} characters"
            )
        chosen = self._machines(machines)
        if not chosen:
            raise TrialError(409, "no machine to record on (no robot is online)")
        with self.lock:
            if self.active is not None:
                raise TrialError(
                    409, f"{self.active['trial_id']} is still {self.active['state']}"
                )
            now = self.clock()
            trial_id = make_trial_id(description, now)
            console_dir = self._console_dir(trial_id)
            try:
                console_dir.mkdir(parents=True, exist_ok=False)
            except OSError as e:
                raise TrialError(500, f"cannot create {console_dir}: {e}") from e
            trial = {
                "schema": SCHEMA,
                "trial_id": trial_id,
                "description": description,
                "state": STARTING,
                "started": round(now, 3),
                "started_at": iso(now),
                "stopped": None,
                "stopped_at": None,
                "console_dir": str(console_dir),
                "events": 0,
                "flags": 0,
                "log_error": None,
                "replace": sorted(set(replace or [])),
                "machines": {
                    m["name"]: _new_machine(m)
                    for m in sorted(chosen, key=lambda x: x["name"])
                },
            }
            self.active = trial
        self.event(
            "trial_start", description=description, machines=sorted(trial["machines"])
        )
        self._save(trial)
        self._publish()
        if self.background:
            threading.Thread(
                target=self._start_all,
                args=(trial, chosen),
                name="trial-start",
                daemon=True,
            ).start()
        else:
            self._start_all(trial, chosen)
        return self.payload()

    def _start_all(self, trial: dict, machines: list[dict]) -> None:
        try:
            self._snapshot(trial, "start")
            self._parallel(lambda m: self._start_machine(trial, m), machines)
            with self.lock:
                states = [x["state"] for x in trial["machines"].values()]
                n_rec = states.count(RECORDING)
                # Stop was pressed while the machines were still starting
                stopped_early = trial["state"] == STOPPING
                if not stopped_early:
                    trial["state"] = RECORDING if n_rec else FAILED
            if stopped_early:
                self.event("trial_started", recording=n_rec, of=len(states),
                           stopped_while_starting=True)  # fmt: skip
                self._stop_started(trial)
                return
            self.event("trial_started", recording=n_rec, of=len(states),
                       not_recording=sorted(n for n, x in trial["machines"].items() if x["state"] != RECORDING))  # fmt: skip
            if not n_rec:
                self._finalize(trial, "no machine started recording")
                return
            self._save(trial)
            self._publish()
            self._ensure_poller()
            self.sleep(CHECK_DELAY_S)
            self._check_topics(trial, machines)
        except Exception:  # noqa: BLE001
            log.exception("starting %s", trial["trial_id"])

    def _stop_started(self, trial: dict, names: list[str] | None = None) -> None:
        """The trial was stopped while machines were starting: the recorders
        that came up after the stop are stopped now (they would go on
        recording with no trial in the console)."""
        with self.lock:
            names = [
                n
                for n, x in trial["machines"].items()
                if x["state"] == RECORDING and (names is None or n in names)
            ]
        for n in names:
            self._set(trial, n, state=STOPPING, message="closing the bag")
        self._publish()
        self._stop_all(trial, self._by_name(names), False)

    def _start_machine(self, trial: dict, m: dict) -> None:
        name = m["name"]
        tset = topic_set(m)
        if tset is None:
            self._set(trial, name, state=SKIPPED,
                      message=f"no topic list for platform {m.get('platform_type')!r}")  # fmt: skip
            return
        args = ["start", "--trial", trial["trial_id"], "--machine", name, "--topic-set", tset,
                "--description", trial["description"], "--base-time", f"{self.clock():.3f}"]  # fmt: skip
        robots = self._robots()
        if robots:
            args += ["--robots", ",".join(robots)]
        reply, t0, t1 = self._rec(m, args, START_TIMEOUT_S)
        replaced = None
        if (
            not reply.get("ok")
            and reply.get("recording")
            and reply.get("trial") != trial["trial_id"]
            and name in (trial.get("replace") or [])
        ):
            # the operator asked for it: close the bag that is being recorded
            # (a clean stop, never a kill), then start this trial's
            replaced = reply.get("trial") or reply.get("dir")
            stop, _, _ = self._rec(
                m, ["stop", "--base-time", f"{self.clock():.3f}"], STOP_TIMEOUT_S
            )
            self.event("recorder_replaced", trial, machine=name, was=replaced,
                       stopped=bool(stop.get("ok")), error=stop.get("error"))  # fmt: skip
            if stop.get("ok"):
                reply, t0, t1 = self._rec(m, args, START_TIMEOUT_S)
            else:
                reply = {"ok": False, "error": f"could not stop {replaced}: "
                         f"{stop.get('error') or 'stop failed'}"}  # fmt: skip
        if reply.get("ok") and reply.get("recording"):
            warnings = []
            if replaced:
                warnings.append(f"stopped the recording of {replaced} to start")
            if reply.get("other_recorders"):
                warnings.append(
                    f"{len(reply['other_recorders'])} other recorder(s) also running on this machine"
                )
            if not reply.get("stack_running") and m.get("role") == "robot":
                warnings.append("the adt4_system session is not running")
            self._set(trial, name, state=RECORDING, message="recording", dir=reply.get("dir"),
                      pid=reply.get("pid"), bytes=reply.get("bytes"), topics=reply.get("topics"),
                      free_bytes=reply.get("free_bytes"), total_bytes=reply.get("total_bytes"),
                      last_seen=round(t1, 3), started=round(t1, 3), warnings=warnings, start=reply)  # fmt: skip
        else:
            self._set(
                trial,
                name,
                state=FAILED,
                message=reply.get("error") or "did not start",
                start=reply,
            )

    def start_machine(self, name: str, replace: bool = False) -> dict:
        """Retry (or add) one machine in the active trial.  ``replace``: a
        recorder already running there is stopped first."""
        with self.lock:
            trial = self.active
            if trial is None or trial["state"] not in (RECORDING, STARTING):
                raise TrialError(409, "no trial is recording")
            if replace and name not in trial.setdefault("replace", []):
                trial["replace"].append(name)
            cur = trial["machines"].get(name)
            if cur and cur["state"] in LIVE:
                raise TrialError(409, f"{name} is {cur['state']}")
        m = next(iter(self._machines([name])))
        with self.lock:
            added = cur is None or bool(cur.get("removed"))
            trial["machines"].setdefault(name, _new_machine(m))
            # a machine that recorded before starts a new part: its topic
            # check and its rate are measured again
            trial["machines"][name].update(
                state=STARTING, message="starting", removed=False,
                topic_check=None, rate_bps=None, sample_t=None, last_growth=None,
            )  # fmt: skip
        if added:
            self.event("machine_add", trial, machine=name)
        self._publish()
        self._start_machine(trial, m)
        with self.lock:
            stopped_early = trial["state"] == STOPPING
        if stopped_early:  # the trial was stopped while this machine started
            self._stop_started(trial, [name])
        self._save(trial)
        self._publish()
        self._ensure_poller()
        return self.payload()

    def stop_recorder(self, name: str) -> dict:
        """Stop whatever recorder runs on a machine that is not recording for
        the active trial: one left behind by a trial this console no longer
        has.  The bag is closed like any other (SIGINT, never a kill)."""
        with self.lock:
            trial = self.active
            cur = trial["machines"].get(name) if trial else None
            if cur and (cur["state"] in LIVE or cur["state"] == STARTING):
                raise TrialError(
                    409,
                    f"{name} is {cur['state']} in {trial['trial_id']}: stop it there",
                )
        m = next(iter(self._machines([name])))
        reply, _, _ = self._rec(
            m, ["stop", "--base-time", f"{self.clock():.3f}"], STOP_TIMEOUT_S
        )
        self.event("recorder_stopped", trial, machine=name, ok=bool(reply.get("ok")),
                   was_recording=reply.get("was_recording"), trial_stopped=reply.get("trial"),
                   error=reply.get("error"))  # fmt: skip
        if not reply.get("ok"):
            raise TrialError(502, f"{name}: {reply.get('error') or 'stop failed'}")
        return {"machine": name, "was_recording": reply.get("was_recording"),
                "trial": reply.get("trial"), "metadata": reply.get("metadata"),
                "size_bytes": reply.get("size_bytes")}  # fmt: skip

    def stop_machine(self, name: str) -> dict:
        """Take one machine out of the active trial: its bag is closed, the
        trial goes on with the others.  ``start_machine`` adds it back (a new
        part next to the closed bag)."""
        with self.lock:
            trial = self.active
            if trial is None or trial["state"] != RECORDING:
                raise TrialError(409, "no trial is recording")
            cur = trial["machines"].get(name)
            if cur is None:
                raise TrialError(404, f"{name} is not in this trial")
            if cur["state"] not in (RECORDING, UNREACHABLE):
                raise TrialError(409, f"{name} is {cur['state']}")
            cur["removed"] = True
        self.event("machine_remove", trial, machine=name)
        self._set(trial, name, state=STOPPING, message="closing the bag")
        self._publish()
        machines = self._by_name([name])

        def run():
            try:
                if machines:
                    reply = self._rec(
                        machines[0], self._stop_args(trial, False), STOP_TIMEOUT_S
                    )[0]
                else:
                    reply = {"ok": False, "unreachable": True}
                self._stop_result(trial, name, reply)
                self._save(trial)
                self._publish()
                self._ensure_poller()  # retries a stop that was not delivered
            except Exception:  # noqa: BLE001
                log.exception("removing %s from %s", name, trial["trial_id"])

        if self.background:
            threading.Thread(target=run, name="trial-remove", daemon=True).start()
        else:
            run()
        return self.payload()

    def _recheck_s(self) -> float:
        return float(getattr(self.settings, "trial_topic_recheck_s", 60.0) or 0.0)

    def _check_topics(self, trial: dict, machines: list[dict]) -> None:
        """Which requested topics have no publisher: a few seconds in, and
        again every ``trial_topic_recheck_s`` (topics come and go when a
        stack is restarted).  A repeat that gets no answer keeps the last
        answer, with its age; the event log gets the changes only."""
        with self.lock:
            rec = [
                m
                for m in machines
                if trial["machines"][m["name"]]["state"] == RECORDING
            ]

        def one(m):
            reply, _, _ = self._rec(m, ["check-topics"], CHECK_TIMEOUT_S)
            return reply

        for name, reply in self._parallel(one, rec).items():
            now = round(self.clock(), 3)
            with self.lock:
                prev = dict(trial["machines"][name].get("topic_check") or {})
                tries = prev.get("tries", 0) + 1
            if not isinstance(reply, dict) or not reply.get("ok"):
                err = reply.get("error") if isinstance(reply, dict) else str(reply)
                if prev.get("ok"):
                    # a repeat without an answer: the last answer stays, the
                    # next repeat is one interval away
                    self._set(trial, name, topic_check={
                        **prev, "tried": now, "recheck_error": err})  # fmt: skip
                    continue
                left = CHECK_TRIES - tries
                self._set(trial, name, topic_check={
                    "ok": False, "tries": tries, "error": err,
                    "retrying": left > 0})  # fmt: skip
                if left <= 0:
                    self.event(
                        "topic_check_failed",
                        trial,
                        machine=name,
                        error=err,
                        tries=tries,
                    )
                continue
            missing = reply.get("missing") or []
            self._set(trial, name, topic_check={
                "ok": True, "tries": tries, "published": reply.get("published"),
                "topics": reply.get("topics"), "missing": missing,
                "checked": now, "tried": now})  # fmt: skip
            if prev.get("ok"):
                before = prev.get("missing") or []
                appeared = [t for t in before if t not in missing]
                gone = [t for t in missing if t not in before]
                if appeared or gone:
                    self.event("topics_changed", trial, machine=name, appeared=appeared,
                               gone=gone, published=reply.get("published"),
                               topics=reply.get("topics"))  # fmt: skip
                continue
            self.event("topic_check", trial, machine=name, published=reply.get("published"),
                       topics=reply.get("topics"), tries=tries)  # fmt: skip
            if missing:
                self.event("topics_missing", trial, machine=name, missing=missing)
        self._save(trial)
        self._publish()

    def _set(self, trial: dict, name: str, **fields) -> None:
        """Update a machine; a change of state is logged as an event."""
        with self.lock:
            cur = trial["machines"][name]
            before = cur["state"]
            cur.update(fields)
            after = cur["state"]
            message = cur.get("message")
        if after != before:
            self.event(
                "machine_state",
                trial,
                machine=name,
                state=after,
                was=before,
                message=message,
            )

    # --------------------------------------------------------------- poll
    def _ensure_poller(self) -> None:
        if not self.background:
            return
        with self.lock:
            if self._thread is None or not self._thread.is_alive():
                self._stop.clear()
                self._thread = threading.Thread(
                    target=self._poll_loop, name="trial-poll", daemon=True
                )
                self._thread.start()

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(self._poll_s())
            self._wake.clear()
            with self.lock:
                trial = self.active
            if trial is None:
                return
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001
                log.exception("trial poll")

    def shutdown(self) -> None:
        """Stop polling (console exit).  Recordings on the machines keep going."""
        self._stop.set()
        self._wake.set()

    def poll_once(self) -> None:
        with self.lock:
            trial = self.active
            if trial is None:
                return
            names = [
                n
                for n, x in trial["machines"].items()
                if x["state"] in LIVE and x["state"] != STOPPING
            ]
            pending = [
                n for n in names if trial["machines"][n]["state"] == STOP_PENDING
            ]
        machines = {m["name"]: m for m in self._by_name(names)}

        def one(m):
            if m["name"] in pending:
                return (
                    "stop",
                    *self._rec(m, self._stop_args(trial, False), STOP_TIMEOUT_S),
                )
            return ("status", *self._rec(m, ["status"], STATUS_TIMEOUT_S))

        res = self._parallel(one, [machines[n] for n in names if n in machines])
        for name, r in res.items():
            if not isinstance(r, tuple):
                continue
            kind, reply, t0, t1 = r
            if kind == "stop":
                self._stop_result(trial, name, reply)
            else:
                self._status_result(trial, name, reply, t0, t1)
        self._retry_topic_checks(trial, machines)
        self._maybe_finalize(trial)
        self._save(trial)
        self._publish()

    def _retry_topic_checks(self, trial: dict, machines: dict) -> None:
        """The topic check must have run on every recording machine: one that
        failed (no answer) is tried again each poll, CHECK_TRIES times in all;
        one that never ran (console restarted) runs now; one that answered is
        asked again every ``trial_topic_recheck_s``."""
        now = self.clock()
        every = self._recheck_s()
        with self.lock:
            if trial["state"] != RECORDING:
                return
            todo = []
            for name, x in trial["machines"].items():
                tc = x.get("topic_check")
                if x["state"] != RECORDING or name not in machines:
                    continue
                never = (
                    tc is None and now - (x.get("started") or now) > 3 * CHECK_DELAY_S
                )
                again = (
                    tc is not None
                    and not tc.get("ok")
                    and tc.get("tries", 0) < CHECK_TRIES
                )
                stale = (
                    tc is not None
                    and bool(tc.get("ok"))
                    and every > 0
                    and now - (tc.get("tried") or tc.get("checked") or 0) >= every
                )
                if never or again or stale:
                    todo.append(machines[name])
        if todo:
            self._check_topics(trial, todo)

    def _status_result(
        self, trial: dict, name: str, r: dict, t0: float, t1: float
    ) -> None:
        with self.lock:
            cur = dict(trial["machines"][name])
        if not r.get("ok") or "recording" not in r:
            if cur["state"] == RECORDING:
                self._set(trial, name, state=UNREACHABLE,
                          message=r.get("error") or "no answer; last known: recording")  # fmt: skip
            return
        fields: dict = {"last_seen": round(t1, 3), "free_bytes": r.get("free_bytes"),
                        "total_bytes": r.get("total_bytes")}  # fmt: skip
        # clock: the machine's time against the middle of the round trip; the
        # sample with the shortest round trip is the best one
        rtt = t1 - t0
        if r.get("t") and (cur.get("clock_rtt_s") is None or rtt <= cur["clock_rtt_s"]):
            fields["clock_offset_s"] = round(r["t"] - (t0 + t1) / 2, 3)
            fields["clock_rtt_s"] = round(rtt, 3)
        same = r.get("recording") and r.get("trial") == trial["trial_id"]
        if cur["state"] not in (RECORDING, UNREACHABLE):
            # a stop that had failed: only notice the recorder going away
            if cur["state"] == STOP_FAILED and not same:
                self._set(trial, name, state=STOPPED, message="the recorder exited after the stop",
                          warnings=["stopped late: check the bag has a metadata.yaml"], **fields)  # fmt: skip
            else:
                self._set(trial, name, **fields)
            return
        if not same:
            if cur["state"] in (RECORDING, UNREACHABLE):
                what = (
                    f"is recording {r.get('trial')}"
                    if r.get("recording")
                    else "is NOT recording"
                )
                self._set(trial, name, state=LOST, message=f"{name} {what} (it was stopped on the machine, or the recorder died)", **fields)  # fmt: skip
            else:
                self._set(trial, name, **fields)
            return
        nbytes, rt = r.get("bytes"), r.get("t")
        prev_b, prev_t = cur.get("bytes"), cur.get("sample_t")
        if nbytes is not None and rt and prev_b is not None and prev_t and rt > prev_t:
            inst = max(0.0, (nbytes - prev_b) / (rt - prev_t))
            old = cur.get("rate_bps")
            fields["rate_bps"] = round(
                inst if old is None else 0.5 * old + 0.5 * inst, 1
            )
            if nbytes > prev_b:
                fields["last_growth"] = round(t1, 3)
        fields.update(
            bytes=nbytes, sample_t=rt, last_write_age_s=r.get("last_write_age_s")
        )
        rate = fields.get("rate_bps", cur.get("rate_bps"))
        secs = estimate(r.get("free_bytes"), rate)
        fields["minutes_left"] = None if secs is None else round(secs / 60, 1)
        warnings = [
            w for w in cur.get("warnings") or [] if not w.startswith(("disk:", "bag:"))
        ]
        free = r.get("free_bytes")
        if (free is not None and free < LOW_DISK_BYTES) or (
            secs is not None and secs < LOW_MINUTES * 60
        ):
            left = "" if secs is None else f", about {secs / 60:.0f} min at this rate"
            warnings.append(f"disk: {free / 1e9:.1f} GB free{left}")
        grew = fields.get("last_growth", cur.get("last_growth"))
        age = t1 - (cur.get("started") or t1)
        steady = age > 0 and (nbytes or 0) / age * STALL_S >= STALL_MIN_BYTES
        if grew and steady and t1 - grew > STALL_S:
            warnings.append(f"bag: has not grown for {t1 - grew:.0f} s")
        had = {w.split(":")[0] for w in cur.get("warnings") or []}
        new = [w for w in warnings if w.split(":")[0] not in had]
        if new:  # each kind of warning is logged once, when it first shows
            self.event("warning", trial, machine=name, warnings=new)
        fields["warnings"] = warnings
        self._set(trial, name, state=RECORDING, message="recording", **fields)

    # --------------------------------------------------------------- flag
    def flag(self, text: str) -> dict:
        text = (text or "").strip()
        if not text:
            raise TrialError(400, "an empty flag")
        if len(text) > MAX_FLAG:
            raise TrialError(400, f"flag is longer than {MAX_FLAG} characters")
        with self.lock:
            trial = self.active
            if trial is None:
                raise TrialError(409, "no trial is recording: start one to add flags")
        ev = self.event(
            "flag",
            trial,
            text=text,
            elapsed_s=round(self.clock() - trial["started"], 1),
        )
        if ev is None:
            raise TrialError(500, f"the flag was NOT saved: {trial.get('log_error')}")
        self._publish()
        return ev

    # --------------------------------------------------------------- stop
    def _stop_args(self, trial: dict, force: bool) -> list:
        args = [
            "stop",
            "--trial",
            trial["trial_id"],
            "--base-time",
            f"{self.clock():.3f}",
        ]
        return args + ["--force"] if force else args

    def stop(self, force: bool = False) -> dict:
        with self.lock:
            trial = self.active
            if trial is None:
                raise TrialError(409, "no trial is recording")
            trial["state"] = STOPPING
            names = [n for n, x in trial["machines"].items() if x["state"] in LIVE]
            for n in names:
                trial["machines"][n]["state_before_stop"] = trial["machines"][n][
                    "state"
                ]
        self.event("trial_stop", trial, force=force, machines=names)
        for n in names:
            self._set(trial, n, state=STOPPING, message="closing the bag")
        self._publish()
        machines = self._by_name(names)
        if self.background:
            threading.Thread(
                target=self._stop_all,
                args=(trial, machines, force),
                name="trial-stop",
                daemon=True,
            ).start()
        else:
            self._stop_all(trial, machines, force)
        return self.payload()

    def _stop_all(self, trial: dict, machines: list[dict], force: bool) -> None:
        try:

            def one(m):
                return self._rec(m, self._stop_args(trial, force), STOP_TIMEOUT_S)[0]

            for name, reply in self._parallel(one, machines).items():
                self._stop_result(trial, name, reply if isinstance(reply, dict) else
                                  {"ok": False, "error": str(reply)})  # fmt: skip
            self._maybe_finalize(trial)
            self._save(trial)
            self._publish()
            self._ensure_poller()  # keeps trying the machines that did not answer
        except Exception:  # noqa: BLE001
            log.exception("stopping %s", trial["trial_id"])

    def _stop_result(self, trial: dict, name: str, r: dict) -> None:
        if r.get("ok"):
            warnings = []
            if r.get("was_recording") is False:
                warnings.append("was not recording when stopped")
            elif not r.get("metadata"):
                warnings.append(
                    "bag closed without metadata.yaml (recover: ros2 bag reindex)"
                )
            silent = r.get("silent_topics") or []
            if silent:
                warnings.append(
                    f"{len(silent)} of {r.get('topics')} topics recorded no message"
                )
            self._set(trial, name, state=STOPPED, message="stopped", stop=r, warnings=warnings,
                      bytes=r.get("size_bytes"), free_bytes=r.get("free_bytes"))  # fmt: skip
            if r.get("size_bytes") and r.get("duration_s"):
                self._remember_rate(name, r["size_bytes"] / max(r["duration_s"], 1.0))
            if warnings:
                self.event(
                    "warning",
                    trial,
                    machine=name,
                    warnings=warnings,
                    silent_topics=silent,
                )
        elif r.get("unreachable"):
            self._set(trial, name, state=STOP_PENDING,
                      message="no answer: it keeps recording; stop is retried until it answers")  # fmt: skip
        elif r.get("recording"):
            self._set(trial, name, state=STOP_FAILED, stop=r,
                      message=(r.get("error") or "still running") + " (Stop harder sends SIGTERM)")  # fmt: skip
        else:
            self._set(
                trial,
                name,
                state=STOP_FAILED,
                message=r.get("error") or "stop failed",
                stop=r,
            )

    def _maybe_finalize(self, trial: dict) -> None:
        with self.lock:
            if trial is not self.active or trial["state"] != STOPPING:
                return
            # a machine that is still starting will have a recorder in a
            # moment: the trial stays open until it has been stopped too
            if any(
                x["state"] in LIVE or x["state"] == STARTING
                for x in trial["machines"].values()
            ):
                return
        self._finalize(trial, None)

    def close(self) -> dict:
        """End a stopping trial although some machine could not be stopped
        (it is left as it is and named in the trial)."""
        with self.lock:
            trial = self.active
            if trial is None:
                raise TrialError(409, "no trial is active")
            if trial["state"] != STOPPING:
                raise TrialError(409, "stop the trial first")
            left = sorted(n for n, x in trial["machines"].items() if x["state"] in LIVE)
        self._finalize(
            trial,
            f"closed with {', '.join(left)} not confirmed stopped" if left else None,
        )
        return self.payload()

    def _finalize(self, trial: dict, note: str | None) -> None:
        now = self.clock()
        self._drain_hub()
        self._snapshot(trial, "stop")
        with self.lock:
            trial["stopped"], trial["stopped_at"] = round(now, 3), iso(now)
            if trial["state"] != FAILED:
                trial["state"] = STOPPED
            trial["note"] = note
            summary = {n: x["state"] for n, x in trial["machines"].items()}
        self.event("trial_end", trial, note=note, machines=summary,
                   duration_s=round(now - trial["started"], 1))  # fmt: skip
        self._save(trial)
        with self.lock:
            if self.active is trial:
                self.active, self.last = None, trial
        self._publish()

    # ------------------------------------------------------------ restore
    def restore(self) -> None:
        """After a console restart: pick the newest unfinished trial up again
        (its machines kept recording; the poll finds out what they do now)."""
        best = None
        try:
            dirs = sorted(self.root.glob(f"trial_*/{CONSOLE_DIR}/{TRIAL_FILE}"))
        except OSError:
            return
        for path in dirs[-50:]:
            try:
                data = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if (
                isinstance(data, dict)
                and data.get("stopped") is None
                and data.get("trial_id")
            ):
                if best is None or data.get("started", 0) > best.get("started", 0):
                    best = data
        if best is None:
            return
        for x in best.get("machines", {}).values():
            if x.get("state") in (STARTING, STOPPING):
                x["state"] = UNREACHABLE
                x["message"] = "console restarted; checking"
        if best.get("state") == STARTING:
            best["state"] = RECORDING
        with self.lock:
            self.active = best
        self.event("console_restarted", best)
        self._ensure_poller()

    # -------------------------------------------------------- request log
    def request_extra(self, path: str) -> dict | None:
        """Read before the request is handled: for an answer to a job's
        question, the job and the question (it is gone once answered)."""
        m = JOB_RE.match(path)
        jobs = getattr(self, "jobs", None)
        if not m or jobs is None:
            return None
        job = jobs.get(m.group(1))
        if job is None:
            return None
        q = job.confirm_request or {}
        return {
            "job": {
                "id": job.id,
                "kind": job.kind,
                "title": job.title,
                "question": q.get("message"),
                "question_kind": q.get("kind"),
                "yes_label": q.get("yes_label"),
                "no_label": q.get("no_label"),
            }
        }

    def log_request(
        self, method: str, path: str, body: bytes, status, response: bytes | None,
        extra: dict | None = None,
    ) -> None:  # fmt: skip
        with self.lock:
            trial = self.active
        if trial is None:
            return
        big = wants_response(path)
        fields = {
            "category": request_category(path),
            "method": method,
            "path": path,
            "status": status,
            "body": summarize_body(body, BIG if big else 20_000),
            **(extra or {}),
        }
        if response is not None:
            try:
                fields["response"] = clip_json(json.loads(response), BIG)
            except ValueError:  # a stream (the chat's NDJSON), or cut short
                fields["response"] = response.decode("utf-8", "replace")
        self.event("request", trial, **fields)

    def log_ui(self, events: list) -> int:
        """What the operator did on the page (clicks, pages, dialogs), sent by
        the browser in batches.  Returns how many were logged."""
        with self.lock:
            trial = self.active
        if trial is None:
            return 0
        n = 0
        for ev in (events or [])[:UI_MAX_EVENTS]:
            if not isinstance(ev, dict) or not ev.get("kind"):
                continue
            clean = {
                k: (v[:500] if isinstance(v, str) else v)
                for k, v in ev.items()
                if k in UI_KEYS
                and k != "kind"
                and isinstance(v, (str, int, float, bool))
            }
            if (
                self.event("ui", trial, action=str(ev["kind"])[:40], **clean)
                is not None
            ):
                n += 1
        return n

    # ------------------------------------------------------ screen video
    def screen_chunk(self, session: str, seq: int, data: bytes, mime: str = "video/webm",
                     final: bool = False, t_client: float | None = None) -> dict:  # fmt: skip
        """Append one piece of a browser's recording of the console page to
        ``console/screen_<session>.<ext>``.  Pieces of one session arrive in
        order (``seq`` 0, 1, ...); a repeated one is ignored."""
        if not SCREEN_RE.match(session or ""):
            raise TrialError(400, "bad screen session id")
        if len(data) > SCREEN_MAX_CHUNK:
            raise TrialError(413, "screen chunk too large")
        now = self.clock()
        with self.lock:
            trial = self.active
            if trial is None and self.last is not None:
                late = now - (self.last.get("stopped") or 0) < SCREEN_LATE_S
                if late and session in (self.last.get("screen") or {}):
                    trial = self.last
            if trial is None:
                raise TrialError(409, "no trial is recording")
            sessions = trial.setdefault("screen", {})
            st = sessions.get(session)
            new = st is None
            if new:
                ext = SCREEN_EXT.get((mime or "").split(";")[0].strip(), "webm")
                st = sessions[session] = {
                    "file": f"screen_{session}.{ext}", "mime": mime, "chunks": 0, "bytes": 0,
                    "started": round(now, 3), "last": None, "ended": None, "gaps": 0,
                }  # fmt: skip
            if seq < st["chunks"]:
                return {"ok": True, "duplicate": True, "next": st["chunks"]}
            gap = seq > st["chunks"]
        path = self._console_dir(trial["trial_id"]) / st["file"]
        if new:
            self.event("screen_start", trial, session=session, file=st["file"], mime=mime,
                       t_client=t_client)  # fmt: skip
        if gap:
            self.event(
                "screen_gap", trial, session=session, expected=st["chunks"], got=seq
            )
        try:
            with open(path, "ab") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
        except OSError as e:
            with self.lock:
                trial["log_error"] = f"{st['file']}: {e}"
            raise TrialError(500, f"screen recording NOT saved: {e}") from e
        with self.lock:
            st["chunks"] = seq + 1
            st["bytes"] += len(data)
            st["last"] = round(now, 3)
            st["gaps"] += 1 if gap else 0
            if final:
                st["ended"] = round(now, 3)
        if final:
            self.event(
                "screen_stop",
                trial,
                session=session,
                file=st["file"],
                bytes=st["bytes"],
            )
            self._save(trial)
            self._publish()
        return {"ok": True, "next": seq + 1, "bytes": st["bytes"], "file": st["file"]}

    def screen_note(self, session: str, what: str, detail: str = "") -> None:
        """The browser says why it is not recording (permission denied,
        sharing stopped)."""
        with self.lock:
            trial = self.active
        if trial is not None:
            self.event("screen_note", trial, session=str(session)[:40], what=str(what)[:40],
                       detail=str(detail)[:300])  # fmt: skip

    # ------------------------------------------------------------ hub tap
    def hub_tap(self, topic: str, data) -> None:
        """Called by the hub on every publish, from any thread: must be
        instant.  The topics worth keeping are queued for the writer."""
        try:
            if self.active is None or not topic.startswith(HUB_PREFIXES):
                return
            self._hub_q.put_nowait((self.clock(), topic, data))
            if self.background and (
                self._hub_thread is None or not self._hub_thread.is_alive()
            ):
                self._hub_thread = threading.Thread(
                    target=self._hub_loop, name="trial-hub", daemon=True
                )
                self._hub_thread.start()
        except Exception:  # noqa: BLE001  a full queue, anything: drop it
            pass

    def _hub_loop(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._hub_q.get(timeout=5.0)
            except queue.Empty:
                if self.active is None:
                    return
                continue
            try:
                self._hub_write(*item)
            except Exception:  # noqa: BLE001
                log.exception("trial hub log")

    def _drain_hub(self) -> None:
        while True:
            try:
                item = self._hub_q.get_nowait()
            except queue.Empty:
                return
            try:
                self._hub_write(*item)
            except Exception:  # noqa: BLE001
                log.exception("trial hub log")

    def _hub_write(self, t: float, topic: str, data) -> None:
        with self.lock:
            trial = self.active
        if trial is None:
            return
        try:
            text = json.dumps(data, sort_keys=True, default=str)
        except (TypeError, ValueError):
            text = str(data)
        digest = hash(text)
        last = self._hub_last.get(topic)
        if last and (last[1] == digest or t - last[0] < HUB_MIN_GAP_S):
            return  # unchanged, or changing faster than is worth a line each
        self._hub_last[topic] = (t, digest)
        self.event("hub", trial, topic=topic, published=round(t, 3),
                   data=clip_json(data, HUB_MAX_CHARS))  # fmt: skip


def _new_machine(m: dict) -> dict:
    return {
        "name": m["name"], "role": m.get("role"), "state": STARTING, "message": "starting",
        "dir": None, "pid": None, "bytes": None, "rate_bps": None, "free_bytes": None,
        "total_bytes": None, "minutes_left": None, "last_seen": None, "warnings": [],
        "clock_offset_s": None, "clock_rtt_s": None, "topic_check": None,
    }  # fmt: skip


def _guard(fn, m):
    try:
        return fn(m)
    except Exception as e:  # noqa: BLE001
        log.exception("trial step on %s", m.get("name"))
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _run_local(cmd: str, timeout: float) -> tuple[int, str, str]:
    try:
        # its own session: the helper's interactive shells must not get this
        # process's terminal (they took its foreground group, and Ctrl-C no
        # longer reached the console)
        p = subprocess.run(["bash", "-c", cmd], stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, errors="replace", timeout=timeout + 10,
                           start_new_session=True)  # fmt: skip
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"
    except OSError as e:
        return -1, "", str(e)
    return p.returncode, p.stdout, p.stderr


# ----------------------------------------------------------- request log
class TrialRequestLog:
    """ASGI middleware: while a trial records, every operator action sent to
    the console (a non-GET ``/api`` request) becomes a ``request`` event: the
    path, the JSON body and the status; for the chat also the answer.

    It only copies bytes that pass through; the request and the response are
    forwarded untouched, and its own errors are logged, never raised."""

    def __init__(self, app, trials=None):
        self.app = app
        self._trials = trials

    def _service(self, scope):
        if self._trials is not None:
            return self._trials
        try:
            return scope["app"].state.services.trials
        except Exception:  # noqa: BLE001
            return None

    async def __call__(self, scope, receive, send):
        try:
            trials = None
            if scope.get("type") == "http" and scope.get("method") in LOG_METHODS:
                path = scope.get("path") or ""
                if path.startswith("/api/") and not any(
                    p in path for p in SKIP_PATH_PARTS
                ):
                    svc = self._service(scope)
                    if svc is not None and svc.active is not None:
                        trials = svc
        except Exception:  # noqa: BLE001
            trials = None
        if trials is None:
            await self.app(scope, receive, send)
            return

        body, resp, status = bytearray(), bytearray(), {}
        keep_resp = wants_response(path)
        try:
            extra = trials.request_extra(path)
        except Exception:  # noqa: BLE001
            extra = None

        # A small body is read here first and handed on unchanged, so it is
        # logged even when the app answers without reading it (a 4xx / 503).
        # A large or unsized one is only copied as the app reads it.
        early: list = []
        try:
            length = int(dict(scope.get("headers") or []).get(b"content-length", b"-1"))
        except ValueError:
            length = -1
        if 0 < length <= MAX_BODY:
            while True:
                msg = await receive()
                early.append(msg)
                if msg.get("type") != "http.request" or not msg.get("more_body"):
                    break

        async def rx():
            msg = early.pop(0) if early else await receive()
            try:
                if msg.get("type") == "http.request" and len(body) < MAX_BODY:
                    body.extend((msg.get("body") or b"")[: MAX_BODY - len(body)])
            except Exception:  # noqa: BLE001
                pass
            return msg

        async def tx(msg):
            try:
                if msg.get("type") == "http.response.start":
                    status["code"] = msg.get("status")
                elif (
                    keep_resp
                    and msg.get("type") == "http.response.body"
                    and len(resp) < MAX_RESPONSE
                ):
                    resp.extend((msg.get("body") or b"")[: MAX_RESPONSE - len(resp)])
            except Exception:  # noqa: BLE001
                pass
            await send(msg)

        try:
            await self.app(scope, rx, tx)
        finally:
            for msg in early:  # the app never asked for the body
                if msg.get("type") == "http.request":
                    body.extend((msg.get("body") or b"")[: MAX_BODY - len(body)])
            try:  # off the event loop: the write is fsynced
                await asyncio.to_thread(trials.log_request, scope["method"], path, bytes(body),
                                        status.get("code"), bytes(resp) if keep_resp else None,
                                        extra)  # fmt: skip
            except Exception:  # noqa: BLE001
                log.exception("trial request log")
