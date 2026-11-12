"""Mission event log: JSONL events with a common envelope (mission logging plan §3).

``EventLog(source)`` appends one JSON object per line to
``<root>/events/<source>.jsonl``; the root is ``$ADT4_RUN_DIR`` when run-adt4 set
it, else the console's base experiment dir (:func:`set_fallback_root`, set by the
logbook).  Every line carries the envelope::

    {"v": 1, "t_wall", "t_mono", "t_ros", "machine", "robot", "run_id",
     "session_id", "mission_id", "goal_id", "plan_id", "source", "type", "seq",
     "payload": {...}}

Writes go through one queue thread (callers never block on the disk), files are
line-buffered appends, fsynced every :data:`FSYNC_S` and rotated at
:data:`ROTATE_BYTES` (``<source>.<n>.jsonl``).  Payloads are redacted by key
(:func:`redact`: api keys, passwords, tokens, authorization; an ``env`` /
``environ`` value is never written) and strings over :data:`BLOB_BYTES` go to a
file under ``blobs/``, referenced as ``{"$file": <path relative to the root>}``.

Pure Python (no ROS).  Off until :func:`enable` (the CLI turns it on; tests
point it at a temporary dir), so library use and tests never write to ``~``.
:func:`emit` never raises: logging must not break the console.
"""

from __future__ import annotations

import base64
import itertools
import json
import logging
import logging.handlers
import os
import pathlib
import queue
import re
import secrets
import socket
import threading
import time

log = logging.getLogger(__name__)

VERSION = 1
ROTATE_BYTES = 200 * 1024 * 1024
FSYNC_S = 2.0
BLOB_BYTES = 64 * 1024
MAX_DEPTH = 12
# a key ending in one of these (case-insensitive, "-" / "_" ignored) is redacted:
# api_key, x-api-key, password, auth_token, Authorization, ... (not max_tokens)
SECRET_KEYS = ("apikey", "password", "passwd", "token", "authorization", "secret", "secretkey",
               "privatekey", "credential", "credentials", "cookie")
# a key equal to one of these is never written (the process env holds secrets)
ENV_KEYS = ("env", "environ", "environment")
REDACTED = "[redacted]"
# secret-looking values, whatever their key (OpenAI / Anthropic keys, bearer tokens)
SECRET_VALUE = re.compile(r"\b(sk-[A-Za-z0-9_\-]{12,}|Bearer\s+[A-Za-z0-9._\-]{8,})")


def _norm_key(k) -> str:
    return str(k).lower().replace("-", "").replace("_", "")


def is_secret_key(k) -> bool:
    n = _norm_key(k)
    return any(n.endswith(s) for s in SECRET_KEYS)


def redact(obj, depth: int = 0):
    """A JSON-able copy of ``obj`` with secret keys redacted and env dumps dropped."""
    if depth > MAX_DEPTH:
        return "[too deep]"
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if str(k).lower() in ENV_KEYS:
                out[str(k)] = "[env omitted]"
            elif is_secret_key(k) and v not in (None, "", False):
                out[str(k)] = REDACTED
            else:
                out[str(k)] = redact(v, depth + 1)
        return out
    if isinstance(obj, (list, tuple, set)):
        return [redact(v, depth + 1) for v in obj]
    if isinstance(obj, str):
        return SECRET_VALUE.sub(REDACTED, obj) if ("sk-" in obj or "Bearer" in obj) else obj
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj
    if isinstance(obj, bytes):
        return {"$bytes": len(obj)}
    if isinstance(obj, pathlib.PurePath):
        return str(obj)
    try:
        return redact(obj.model_dump(), depth + 1)  # pydantic
    except Exception:  # noqa: BLE001
        return str(obj)


# ---------------------------------------------------------------- context
class Context:
    """Ids every event is stamped with (machine, run, session, current mission)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.machine = os.environ.get("ADT4_MACHINE") or socket.gethostname()
        self.robot = os.environ.get("ADT4_ROBOT_NAME") or self.machine
        self.run_id = _default_run_id(self.machine)
        self.session_id: str | None = os.environ.get("ADT4_SESSION_ID") or None
        self.mission_id: str | None = None
        self.ros_clock = None  # () -> float seconds (sim-time aware); None: wall time

    def ids(self) -> dict:
        with self.lock:
            return {
                "machine": self.machine,
                "robot": self.robot,
                "run_id": self.run_id,
                "session_id": self.session_id,
                "mission_id": self.mission_id,
            }

    def t_ros(self, t_wall: float) -> float:
        fn = self.ros_clock
        if fn is None:
            return t_wall
        try:
            return float(fn())
        except Exception:  # noqa: BLE001
            return t_wall


def _default_run_id(machine: str) -> str:
    rid = os.environ.get("ADT4_RUN_ID")
    if rid:
        return rid
    rd = os.environ.get("ADT4_RUN_DIR")
    if rd:
        return pathlib.Path(rd).name
    return f"{machine}_{time.strftime('%Y%m%d-%H%M%S')}_{secrets.token_hex(2)}"


CONTEXT = Context()


# ---------------------------------------------------------------- writer
class _Writer:
    """One thread that owns every open event file."""

    def __init__(self, rotate_bytes=ROTATE_BYTES, fsync_s=FSYNC_S):
        self.q: queue.Queue = queue.Queue()
        self.rotate_bytes = rotate_bytes
        self.fsync_s = fsync_s
        self.files: dict[pathlib.Path, object] = {}
        self.dirty: set[pathlib.Path] = set()
        self.last_sync = time.monotonic()
        self.errors = 0
        self.thread = threading.Thread(target=self._run, name="eventlog-writer", daemon=True)
        self.thread.start()

    def put(self, path: pathlib.Path, line: str) -> None:
        self.q.put((path, line))

    def flush(self, timeout: float = 5.0) -> None:
        """Wait until every queued line is written and synced (tests, shutdown)."""
        done = threading.Event()
        self.q.put(("__flush__", done))
        done.wait(timeout)

    def _file(self, path: pathlib.Path):
        f = self.files.get(path)
        if f is None:
            path.parent.mkdir(parents=True, exist_ok=True)
            f = open(path, "a", buffering=1, encoding="utf-8")  # noqa: SIM115 — line buffered
            self.files[path] = f
        return f

    def _rotate(self, path: pathlib.Path) -> None:
        f = self.files.pop(path, None)
        if f is not None:
            f.close()
        stem = path.name[: -len(".jsonl")] if path.name.endswith(".jsonl") else path.name
        n = 1
        while (path.parent / f"{stem}.{n}.jsonl").exists():
            n += 1
        os.replace(path, path.parent / f"{stem}.{n}.jsonl")

    def _sync(self) -> None:
        for p in list(self.dirty):
            f = self.files.get(p)
            if f is not None:
                try:
                    f.flush()
                    os.fsync(f.fileno())
                except OSError:
                    pass
        self.dirty.clear()
        self.last_sync = time.monotonic()

    def _run(self) -> None:
        while True:
            try:
                item = self.q.get(timeout=self.fsync_s)
            except queue.Empty:
                if self.dirty:
                    self._sync()
                continue
            path, line = item
            if path == "__flush__":
                self._sync()
                line.set()
                continue
            try:
                f = self._file(path)
                f.write(line)
                self.dirty.add(path)
                if f.tell() >= self.rotate_bytes:
                    f.flush()
                    os.fsync(f.fileno())
                    self.dirty.discard(path)
                    self._rotate(path)
            except Exception as e:  # noqa: BLE001
                self.errors += 1
                if self.errors <= 5 or self.errors % 1000 == 0:
                    log.warning("event log write to %s failed: %s", path, e)
            if time.monotonic() - self.last_sync >= self.fsync_s:
                self._sync()

    def close_all(self) -> None:
        self.flush()
        for f in self.files.values():
            try:
                f.close()
            except OSError:
                pass
        self.files.clear()


# ---------------------------------------------------------------- module state
_lock = threading.RLock()
_enabled = False
_fallback_root: pathlib.Path | None = None
_writer: _Writer | None = None
_logs: dict[str, EventLog] = {}
_hooks: list = []  # fn(type, payload, kw) run synchronously before an event is stamped
_listeners: list = []  # fn(record) run after an event is queued
_rotate_bytes = ROTATE_BYTES


def enable(on: bool = True, fallback_root=None, rotate_bytes: int | None = None) -> None:
    """Turn file logging on (the CLI; tests with a temporary ``fallback_root``)."""
    global _enabled, _rotate_bytes
    with _lock:
        _enabled = on
        if rotate_bytes is not None:
            _rotate_bytes = rotate_bytes
            if _writer is not None:
                _writer.rotate_bytes = rotate_bytes
        if fallback_root is not None:
            set_fallback_root(fallback_root)


def enabled() -> bool:
    return _enabled


def set_fallback_root(path) -> None:
    """The root used when ``$ADT4_RUN_DIR`` is unset (the base experiment dir)."""
    global _fallback_root
    with _lock:
        _fallback_root = pathlib.Path(os.path.expanduser(str(path))) if path else None


def root() -> pathlib.Path | None:
    """Where events go: ``$ADT4_RUN_DIR``, else the fallback root; None: nowhere."""
    rd = os.environ.get("ADT4_RUN_DIR")
    if rd:
        return pathlib.Path(os.path.expanduser(rd))
    return _fallback_root


def _get_writer() -> _Writer:
    global _writer
    with _lock:
        if _writer is None:
            _writer = _Writer(rotate_bytes=_rotate_bytes)
        return _writer


def flush(timeout: float = 5.0) -> None:
    if _writer is not None:
        _writer.flush(timeout)


def reset() -> None:
    """Tests: close files, forget sources, hooks and ids, and turn logging off."""
    global _enabled, _fallback_root, _writer, _rotate_bytes
    with _lock:
        if _writer is not None:
            _writer.close_all()
        _writer = None
        _enabled = False
        _rotate_bytes = ROTATE_BYTES
        _fallback_root = None
        _logs.clear()
        _hooks.clear()
        _listeners.clear()
        with CONTEXT.lock:
            CONTEXT.session_id = None
            CONTEXT.mission_id = None
        CONTEXT.ros_clock = None


def add_hook(fn) -> None:
    """``fn(type, payload, kw)`` before each event is stamped (it may open a mission)."""
    with _lock:
        if fn not in _hooks:
            _hooks.append(fn)


def remove_hook(fn) -> None:
    with _lock:
        if fn in _hooks:
            _hooks.remove(fn)


def add_listener(fn) -> None:
    """``fn(record)`` after each event (on the emitting thread; must be quick)."""
    with _lock:
        if fn not in _listeners:
            _listeners.append(fn)


def remove_listener(fn) -> None:
    with _lock:
        if fn in _listeners:
            _listeners.remove(fn)


def set_ids(**ids) -> None:
    """Set ``session_id`` / ``mission_id`` / ``run_id`` / ``machine`` / ``robot``."""
    with CONTEXT.lock:
        for k, v in ids.items():
            if k not in ("session_id", "mission_id", "run_id", "machine", "robot"):
                raise KeyError(k)
            setattr(CONTEXT, k, v)


def get(source: str) -> EventLog:
    with _lock:
        lg = _logs.get(source)
        if lg is None:
            lg = _logs[source] = EventLog(source)
        return lg


def emit(source: str, type: str, payload=None, **kw) -> dict | None:  # noqa: A002
    """``get(source).emit(...)``; never raises."""
    try:
        return get(source).emit(type, payload, **kw)
    except Exception:  # noqa: BLE001
        log.exception("event %s/%s", source, type)
        return None


def _safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(s))[:80] or "x"


class EventLog:
    """One source's events (``<root>/events/<source>.jsonl``)."""

    def __init__(self, source: str):
        self.source = _safe_name(source)
        self._seq = itertools.count(1)
        self._blob_seq = itertools.count(1)

    def path(self) -> pathlib.Path | None:
        r = root()
        return None if r is None else r / "events" / f"{self.source}.jsonl"

    def emit(self, type: str, payload=None, *, goal_id=None, plan_id=None, robot=None,  # noqa: A002
             t_wall: float | None = None, **ids) -> dict:  # fmt: skip
        """Stamp and queue one event; returns the record (also when logging is off)."""
        payload = {} if payload is None else payload
        kw = {"goal_id": goal_id, "plan_id": plan_id, "robot": robot, **ids}
        for h in list(_hooks):
            try:
                h(type, payload, kw)
            except Exception:  # noqa: BLE001
                log.exception("event hook for %s", type)
        t_wall = time.time() if t_wall is None else t_wall
        env = CONTEXT.ids()
        if robot:
            env["robot"] = robot
        for k in ("session_id", "mission_id", "run_id"):
            if ids.get(k) is not None:
                env[k] = ids[k]
        rec = {
            "v": VERSION,
            "t_wall": t_wall,
            "t_mono": time.monotonic(),
            "t_ros": CONTEXT.t_ros(t_wall),
            **env,
            "goal_id": goal_id,
            "plan_id": plan_id,
            "source": self.source,
            "type": type,
            "seq": next(self._seq),
            "payload": self._blobs(redact(payload)),
        }
        path = self.path() if _enabled else None
        if path is not None:
            try:
                line = json.dumps(rec, default=str, separators=(",", ":")) + "\n"
            except (TypeError, ValueError) as e:
                line = json.dumps({**rec, "payload": {"unserializable": str(e)}}, default=str) + "\n"
            _get_writer().put(path, line)
        for fn in list(_listeners):
            try:
                fn(rec)
            except Exception:  # noqa: BLE001
                log.exception("event listener for %s", type)
        return rec

    # ------------------------------------------------------------ blobs
    def _blobs(self, obj, depth: int = 0):
        """Strings over BLOB_BYTES move to files: ``{"$file": ...}``."""
        if depth > MAX_DEPTH:
            return obj
        if isinstance(obj, dict):
            return {k: self._blobs(v, depth + 1) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._blobs(v, depth + 1) for v in obj]
        if isinstance(obj, str) and len(obj) > BLOB_BYTES:
            data, ext = _blob_bytes(obj)
            ref = self.put_file(f"{next(self._blob_seq)}{ext}", data)
            return ref or {"$dropped": len(obj)}
        return obj

    def put_file(self, name: str, data: bytes, subdir: str | None = None, dest: pathlib.Path | None = None):
        """Write ``data`` (synchronously) and return its ``{"$file": path}`` ref.

        ``dest``: the exact file (e.g. a mission's ``images/goals/...``), else
        ``<root>/blobs/<source>/<name>``.  The path is relative to the event root
        when the file is under it, else absolute.  None when logging is off."""
        r = root()
        if not _enabled or (r is None and dest is None):
            return None
        if dest is None:
            dest = r / "blobs" / (subdir or self.source) / _safe_name(name)
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        except OSError as e:
            log.warning("event blob %s: %s", dest, e)
            return None
        return {"$file": rel_ref(dest), "bytes": len(data)}


def append_raw(relpath: str, lines) -> int:
    """Queue already-formed JSONL ``lines`` (str or dict records) to
    ``<root>/events/<relpath>``: the robots' live C7 events land in
    ``events/live/<robot>.jsonl`` as the robots wrote them (their own envelope).
    Returns how many were queued (0 while logging is off)."""
    r = root()
    if not _enabled or r is None:
        return 0
    parts = [_safe_name(x) for x in str(relpath).split("/") if x not in ("", ".", "..")]
    if not parts:
        return 0
    path = r.joinpath("events", *parts)
    w = _get_writer()
    n = 0
    for ln in lines or []:
        if isinstance(ln, dict):
            ln = json.dumps(redact(ln), default=str, separators=(",", ":"))
        ln = str(ln).rstrip("\n")
        if not ln:
            continue
        if "sk-" in ln or "Bearer" in ln:
            ln = SECRET_VALUE.sub(REDACTED, ln)
        w.put(path, ln + "\n")
        n += 1
    return n


def rel_ref(path: pathlib.Path) -> str:
    r = root()
    try:
        return str(path.relative_to(r)) if r is not None else str(path)
    except ValueError:
        return str(path)


def _blob_bytes(s: str) -> tuple[bytes, str]:
    """A data URI's bytes (and extension), else the string as UTF-8 text."""
    if s.startswith("data:") and ";base64," in s[:100]:
        head, b64 = s.split(",", 1)
        mime = head[5:].split(";")[0]
        ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}.get(mime, ".bin")
        try:
            return base64.b64decode(b64), ext
        except ValueError:
            pass
    return s.encode("utf-8", "replace"), ".txt"


# ---------------------------------------------------------------- console log
_console_handler: logging.Handler | None = None
# attach_console_log() was called before any root was known (a console started
# without $ADT4_RUN_DIR: the base dir comes with the logbook / first session);
# the logbook attaches it as soon as it sets the fallback root.
_console_wanted = False


def console_log_path() -> pathlib.Path | None:
    r = root()
    return None if r is None else r / "console.log"


def attach_console_log(path=None, max_bytes: int = 50 * 1024 * 1024, backups: int = 5) -> pathlib.Path | None:
    """Mirror the console's own log to a rotating file (``$ADT4_RUN_DIR/console.log``
    or the base experiment dir); re-attaching moves it.  Without a root yet it
    returns None and the next :func:`set_fallback_root` caller attaches it."""
    global _console_handler, _console_wanted
    _console_wanted = True
    path = pathlib.Path(path) if path else console_log_path()
    if path is None:
        return None
    with _lock:
        cur = _console_handler
        if cur is not None and getattr(cur, "baseFilename", None) == str(path.resolve()):
            return path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            h = logging.handlers.RotatingFileHandler(path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
        except OSError as e:
            log.warning("console log file %s: %s", path, e)
            return None
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        rootlog = logging.getLogger()
        if cur is not None:
            rootlog.removeHandler(cur)
            cur.close()
        rootlog.addHandler(h)
        _console_handler = h
    return path


def detach_console_log() -> None:
    global _console_handler, _console_wanted
    _console_wanted = False
    with _lock:
        if _console_handler is not None:
            logging.getLogger().removeHandler(_console_handler)
            _console_handler.close()
            _console_handler = None
