"""``adt4-mission``: mission folders from the shell.

    adt4-mission check <mission_dir|mission_id> [--json] [--root ~/adt4_output]
    adt4-mission list [--root ~/adt4_output] [--json]
    adt4-mission manifest|index <mission_dir|mission_id>
    adt4-mission assemble <mission_dir|mission_id>   (plans, run ids, manifest, index, check)
    adt4-mission export <mission_dir|mission_id> [--out DIR] [--bags]
    adt4-mission slice ...                           (= adt4-mission-slice)

``check`` exits 1 on FAIL (0 on PASS / WARN) and writes ``report.json``.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

from . import check, index, manifest, slicer, store


def resolve(target: str, root: str) -> pathlib.Path:
    p = pathlib.Path(os.path.expanduser(target))
    if (p / "mission.json").exists():
        return p
    found = store.find_mission(os.path.expanduser(root), target)
    if found is None:
        raise SystemExit(
            f"no mission {target!r} (not a mission dir, nor under {root}/*/missions/)"
        )
    return found


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "slice":
        return slicer.main(argv[1:])
    p = argparse.ArgumentParser(
        prog="adt4-mission", description=__doc__.split("\n\n")[0]
    )
    p.add_argument(
        "cmd", choices=["check", "list", "manifest", "index", "assemble", "export"]
    )
    p.add_argument("target", nargs="?")
    p.add_argument(
        "--root", default=os.environ.get("ADT4_OUTPUT_ROOT", "~/adt4_output")
    )
    p.add_argument("--json", action="store_true")
    p.add_argument("--out", default=None)
    p.add_argument("--bags", action="store_true")
    a = p.parse_args(argv)
    if a.cmd == "list":
        sessions = store.list_sessions(os.path.expanduser(a.root), sizes=False)
        if a.json:
            print(json.dumps(sessions, indent=1, default=str))
            return 0
        for s in sessions:
            print(
                f"session {s['session_id']}  {s.get('experiment')}  robots={','.join(s.get('robots') or [])}"
            )
            for m in s["missions"]:
                print(f"  {m['mission_id']}  {m.get('name') or '':20} {m.get('duration_s')} s  goals={m['n_goals']}"
                      f"  check={m.get('check')}  collect={m.get('collect')}")  # fmt: skip
        return 0
    if not a.target:
        p.error(f"{a.cmd} needs a mission dir or id")
    mdir = resolve(a.target, a.root)
    if a.cmd == "check":
        rep = check.run(mdir)
        print(
            json.dumps(rep, indent=1, default=str)
            if a.json
            else check.format_table(rep)
        )
        return 1 if rep["status"] == check.FAIL else 0
    if a.cmd == "manifest":
        m = manifest.build(mdir)
        print(
            json.dumps(m, indent=1, default=str) if a.json else f"{mdir}/manifest.json"
        )
        return 0
    if a.cmd == "index":
        print(index.build(mdir))
        return 0
    if a.cmd == "assemble":
        from .collector import Collector

        out = Collector().assemble(mdir)
        print(json.dumps(out, indent=1, default=str))
        return 0
    if a.cmd == "export":
        try:
            out = store.export(
                mdir,
                pathlib.Path(a.out or mdir.parent.parent / "exports"),
                include_bags=a.bags,
            )
        except PermissionError as e:
            print(f"export refused: {e}", file=sys.stderr)
            return 1
        print(out)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
