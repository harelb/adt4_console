"""Find the rosbag recordings running on this machine.  Runs ON THE ROBOT.

The console sends this file's source over ssh (``python3 -``, stdlib only) and
reads one JSON line back: ``{"recordings": [...]}``.  A recording is a
``ros2 bag record`` process (or a ``rosbag2*`` recorder whose parent is not
one); its tmux pane is found by walking its parents up to a ``pane_pid``, so a
recording in any window or session is found, not only ``adt4_system:recording``.

The pure part (:func:`scan`) takes the process table and the pane list, so it
is tested on fakes; :func:`main` reads ``/proc`` and ``tmux list-panes -a``.
"""

import json
import os
import subprocess
import time

PANE_FORMAT = "#{pane_id}\t#{pane_pid}\t#{session_name}\t#{window_name}\t#{window_index}.#{pane_index}"


def is_recorder(argv):
    """``.../ros2 bag record ...`` or a ``rosbag2*`` recorder binary."""
    for i, a in enumerate(argv):
        if os.path.basename(a) == "ros2" and argv[i + 1 : i + 3] == ["bag", "record"]:
            return True
    return (
        bool(argv)
        and os.path.basename(argv[0]).startswith("rosbag2")
        and "record" in " ".join(argv)
    )


def output_arg(argv):
    """The ``-o`` / ``--output`` value of a ``ros2 bag record`` command line."""
    for i, a in enumerate(argv):
        if a in ("-o", "--output") and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--output="):
            return a.split("=", 1)[1]
    return None


def parse_panes(text):
    """``tmux list-panes -a -F PANE_FORMAT`` -> {pane_pid: pane dict}."""
    panes = {}
    for line in (text or "").splitlines():
        parts = line.split("\t")
        if len(parts) != 5 or not parts[1].isdigit():
            continue
        pane_id, pid, session, window, index = parts
        panes[int(pid)] = {
            "pane": pane_id,
            "session": session,
            "window": window,
            "target": "%s:%s" % (session, index),
        }
    return panes


def scan(procs, panes):
    """``procs``: {pid: {"ppid", "argv", "cwd", "started", "fds"}}; ``panes``:
    :func:`parse_panes`.  Returns the top-level recorders (a recorder whose
    ancestor is also one is that recorder's child) without sizes."""
    rec = {pid for pid, p in procs.items() if is_recorder(p.get("argv") or [])}
    out = []
    for pid in sorted(rec):
        ancestors, cur = [], procs[pid].get("ppid")
        while cur and cur in procs and cur not in ancestors and len(ancestors) < 64:
            ancestors.append(cur)
            cur = procs[cur].get("ppid")
        if any(a in rec for a in ancestors):
            continue
        p = procs[pid]
        output = output_arg(p.get("argv") or [])
        if output and not os.path.isabs(output) and p.get("cwd"):
            output = os.path.normpath(os.path.join(p["cwd"], output))
        if not output:  # no -o: the directory of a bag file it (or a child) holds open
            for q in [pid] + [c for c, cp in procs.items() if cp.get("ppid") == pid]:
                f = next(
                    (
                        f
                        for f in procs.get(q, {}).get("fds") or []
                        if f.endswith((".mcap", ".db3"))
                    ),
                    None,
                )
                if f:
                    output = os.path.dirname(f)
                    break
        pane = next((panes[a] for a in [pid] + ancestors if a in panes), None)
        out.append(
            {
                "pid": pid,
                "pane": pane["pane"] if pane else None,
                "target": pane["target"] if pane else None,
                "session": pane["session"] if pane else None,
                "window": pane["window"] if pane else None,
                "output": output,
                "started": p.get("started"),
            }
        )
    return out


def dir_size(path):
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def _read(path, mode="r"):
    try:
        with open(path, mode) as f:
            return f.read()
    except OSError:
        return None


def read_procs():
    btime = 0.0
    for line in (_read("/proc/stat") or "").splitlines():
        if line.startswith("btime "):
            btime = float(line.split()[1])
    hz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
    me = os.getpid()
    procs = {}
    for d in os.listdir("/proc"):
        if not d.isdigit() or int(d) == me:
            continue
        stat = _read("/proc/%s/stat" % d)
        if not stat:
            continue
        rest = stat[stat.rfind(")") + 2 :].split()  # comm may hold spaces
        procs[int(d)] = {
            "ppid": int(rest[1]),
            "started": round(btime + int(rest[19]) / hz, 1),
            "argv": None,
        }
        raw = _read("/proc/%s/cmdline" % d, "rb")
        procs[int(d)]["argv"] = [
            a.decode("utf-8", "replace") for a in (raw or b"").split(b"\0") if a
        ]
    for pid, p in procs.items():
        if is_recorder(p["argv"]) or is_recorder(
            procs.get(p["ppid"], {}).get("argv") or []
        ):
            try:
                p["cwd"] = os.readlink("/proc/%d/cwd" % pid)
                fd_dir = "/proc/%d/fd" % pid
                p["fds"] = [
                    os.readlink(os.path.join(fd_dir, f)) for f in os.listdir(fd_dir)
                ]
            except OSError:
                p.setdefault("fds", [])
    return procs


def main():
    try:
        panes_text = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", PANE_FORMAT],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=5,
        ).stdout  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        panes_text = ""
    recs = scan(read_procs(), parse_panes(panes_text))
    for r in recs:
        out = r["output"]
        r["size_bytes"] = dir_size(out) if out and os.path.isdir(out) else None
        r["metadata"] = bool(out) and os.path.isfile(os.path.join(out, "metadata.yaml"))
    print(json.dumps({"recordings": recs, "t": round(time.time(), 1)}))


if __name__ == "__main__":
    main()
