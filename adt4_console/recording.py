"""Rosbag recordings on fleet machines: detect them, and stop one cleanly.

Detection runs :mod:`recording_probe` on the machine over ssh.  Stopping sends
Ctrl-C to the recorder's own tmux pane only (``ros2 bag record`` then closes the
mcap and writes ``metadata.yaml``), never to the session's other panes, and
never ``kill -9`` or ``tmux kill-session``: the rest of the stack keeps running.
"""

from __future__ import annotations

import base64
import json
import logging
import pathlib
import re
import shlex
import time

log = logging.getLogger(__name__)

MARKER = "---REC---"
PANE_RE = re.compile(r"^%\d+$")

# Stop: how long the recorder gets to close its bag after each signal
STOP_WAIT_S = 30.0
HARDER_SIGINT_WAIT_S = 10.0
STOP_POLL_S = 2.0

_PROBE_SRC = pathlib.Path(__file__).with_name("recording_probe.py").read_text()

NOT_RECORDING = {
    "active": False,
    "pane": None,
    "output": None,
    "started": None,
    "size_bytes": None,
}


def probe_command() -> str:
    """Shell command that prints MARKER then the probe's JSON line."""
    b64 = base64.b64encode(_PROBE_SRC.encode()).decode()
    return f"echo '{MARKER}'; echo '{b64}' | base64 -d | python3 -"


def parse_probe(stdout: str) -> list[dict] | None:
    """The recordings in a probe's output; None when the output has no probe
    result (ssh failed, no python3 ...)."""
    text = stdout or ""
    if MARKER in text:
        text = text.split(MARKER, 1)[1]
    for line in reversed(text.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                recs = json.loads(line).get("recordings")
            except (ValueError, AttributeError):
                return None
            return (
                [r for r in recs if isinstance(r, dict)]
                if isinstance(recs, list)
                else None
            )
    return None


def summarize(recs: list[dict] | None) -> dict:
    """The status poll's ``recording`` field: the first recording (one owned
    by a tmux pane first), plus ``count`` and every recording under ``all``."""
    if recs is None:
        return {**NOT_RECORDING, "error": "recording probe failed"}
    if not recs:
        return dict(NOT_RECORDING)
    first = sorted(recs, key=lambda r: r.get("pane") is None)[0]
    return {
        "active": True,
        "pane": first.get("pane"),
        "output": first.get("output"),
        "started": first.get("started"),
        "size_bytes": first.get("size_bytes"),
        "pid": first.get("pid"),
        "target": first.get("target"),
        "metadata": first.get("metadata"),
        "count": len(recs),
        "all": recs,
    }


def probe(ssh, user: str, ip: str, timeout: float = 10) -> list[dict] | None:
    rc, out, _ = ssh(user, ip, probe_command(), timeout=timeout)
    return parse_probe(out) if rc == 0 else None


# ------------------------------------------------------------------ stop
def ctrl_c_command(pane: str) -> str:
    if not PANE_RE.match(pane or ""):
        raise ValueError(f"bad tmux pane id {pane!r}")
    return f"tmux send-keys -t {pane} C-c"


def alive_command(pid: int) -> str:
    pid = int(pid)
    return f"if kill -0 {pid} 2>/dev/null; then echo RUNNING; else echo GONE; fi"


def sigterm_command(pid: int) -> str:
    return f"kill -TERM {int(pid)}"


def bag_info_command(output: str) -> str:
    q = shlex.quote(output)
    return (
        f"du -sb -- {q} 2>/dev/null | cut -f1; "
        f"if [ -f {q}/metadata.yaml ]; then echo METADATA_OK; else echo METADATA_MISSING; fi"
    )


def fmt_bytes(n) -> str:
    """Decimal units, as the console UI shows sizes."""
    if n is None:
        return "?"
    n, i, units = float(n), 0, ("B", "kB", "MB", "GB", "TB")
    while n >= 1000 and i < len(units) - 1:
        n, i = n / 1000, i + 1
    return f"{n:.0f} {units[i]}" if i == 0 or n >= 100 else f"{n:.1f} {units[i]}"


def _wait_exit(ssh, user, ip, pid, wait_s, poll_s, job) -> bool:
    """Poll until ``pid`` is gone; True when it exited within ``wait_s``."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < wait_s:
        job.check_cancelled()
        time.sleep(poll_s)
        rc, out, err = ssh(user, ip, alive_command(pid), timeout=10)
        if "GONE" in (out or ""):
            job.log("ok", f"recorder exited after {time.monotonic() - t0:.0f} s")
            return True
        if "RUNNING" not in (out or ""):
            job.log("dim", f"could not check the recorder: {err or out or rc}")
    return False


def stop_recording(ssh, user: str, ip: str, robot: str, job, *, pane: str | None = None,
                   wait_s: float | None = None, harder_wait_s: float | None = None,
                   poll_s: float | None = None) -> dict | bool:  # fmt: skip
    """Stop ``robot``'s recording inside ``job``: Ctrl-C its pane, wait up to
    ``wait_s``; if it is still running, ask (job confirmation) to stop harder:
    a second Ctrl-C, then SIGTERM to the recorder.  The job result: a dict
    (``stopped``, ``output``, ``size_bytes``, ``metadata``) or False."""
    wait_s = STOP_WAIT_S if wait_s is None else wait_s
    harder_wait_s = HARDER_SIGINT_WAIT_S if harder_wait_s is None else harder_wait_s
    poll_s = STOP_POLL_S if poll_s is None else poll_s
    recs = probe(ssh, user, ip)
    if recs is None:
        job.log("error", f"{robot}: could not list its recordings over ssh")
        return False
    if pane is not None:
        recs = [r for r in recs if r.get("pane") == pane]
    rec = next((r for r in recs if r.get("pane")), None)
    if rec is None:
        if recs:
            job.log(
                "error",
                f"{robot}: the recorder (pid {recs[0].get('pid')}) is not in a tmux pane; stop it on the robot",
            )
            return False
        job.log(
            "info",
            f"{robot}: no bag is recording" + (f" in pane {pane}" if pane else ""),
        )
        return {"stopped": False, "was_recording": False}
    pane, pid, output = rec["pane"], int(rec["pid"]), rec.get("output") or ""
    where = f"{rec.get('target') or pane} ({pane})"
    job.log("info", f"{robot}: recording to {output or '?'} in pane {where}, pid {pid}")

    rc, out, err = ssh(user, ip, ctrl_c_command(pane), timeout=10)
    if rc != 0:
        job.log("error", f"{robot}: Ctrl-C to pane {pane} failed: {err or out or rc}")
        return False
    job.log(
        "info",
        f"Sent Ctrl-C to pane {pane}; waiting up to {wait_s:.0f} s for ros2 bag to close the bag ...",
    )
    exited = _wait_exit(ssh, user, ip, pid, wait_s, poll_s, job)
    signals = ["SIGINT"]
    if not exited:
        job.log("warn", f"{robot}: the recorder is still running after {wait_s:.0f} s")
        harder = job.confirm(
            f"The recorder on {robot} is still running {wait_s:.0f} s after Ctrl-C "
            f"(it may still be writing {output or 'the bag'}). Stop harder: a second Ctrl-C, "
            f"then SIGTERM to the recorder (pid {pid}) only?",
            kind="stop_bag_harder",
            yes_label="Stop harder",
            no_label="Leave it running",
            detail={"robot": robot, "pane": pane, "pid": pid, "output": output},
        )
        if not harder:
            job.log("warn", f"{robot}: left the recorder running in pane {where}")
            _event(robot, pane, output, "still running")
            return False
        rc, out, err = ssh(user, ip, ctrl_c_command(pane), timeout=10)
        signals.append("SIGINT")
        job.log(
            "info",
            f"Sent a second Ctrl-C to pane {pane}; waiting up to {harder_wait_s:.0f} s ...",
        )
        exited = _wait_exit(ssh, user, ip, pid, harder_wait_s, poll_s, job)
        if not exited:
            rc, out, err = ssh(user, ip, sigterm_command(pid), timeout=10)
            signals.append("SIGTERM")
            job.log(
                "info", f"Sent SIGTERM to pid {pid}; waiting up to {wait_s:.0f} s ..."
            )
            exited = _wait_exit(ssh, user, ip, pid, wait_s, poll_s, job)
        if not exited:
            job.log(
                "error",
                f"{robot}: the recorder (pid {pid}) is still running; check pane {where} on the robot (not killing it with -9)",
            )
            _event(robot, pane, output, "still running after SIGTERM")
            return False

    size, metadata = None, None
    if output:
        rc, out, _ = ssh(user, ip, bag_info_command(output), timeout=20)
        lines = (out or "").split()
        size = int(lines[0]) if lines and lines[0].isdigit() else None
        metadata = "METADATA_OK" in (out or "")
    if metadata:
        job.log(
            "ok",
            f"{robot}: bag closed: {output} ({fmt_bytes(size)}), metadata.yaml written",
        )
    else:
        job.log("warn", f"{robot}: recorder stopped but {output or 'the bag'} has no metadata.yaml "
                f"({fmt_bytes(size)}); recover it with: ros2 bag reindex {output or '<bag dir>'}")  # fmt: skip
    _event(
        robot,
        pane,
        output,
        f"stopped ({'+'.join(signals)}), {fmt_bytes(size)}, metadata={'ok' if metadata else 'missing'}",
    )
    return {
        "stopped": True,
        "robot": robot,
        "pane": pane,
        "output": output,
        "size_bytes": size,
        "metadata": metadata,
        "signals": signals,
    }


def _event(robot, pane, output, what) -> None:
    """One console-log line per stop (the console has no mission event log)."""
    log.info("recording stop: robot=%s pane=%s bag=%s: %s", robot, pane, output, what)
