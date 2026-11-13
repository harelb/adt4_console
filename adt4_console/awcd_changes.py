"""The AWCD base station's change set, for the 3D overlay and the change list.

``awcd_base_station`` (dcist_launch_system) merges every robot's change
detection reports (``/awcd_changes``, relayed up by fleetlink) and publishes,
latched, the merged set on ``<base>/awcd_base_station/reconciled_changes``
(``khronos_msgs/AwcdChanges``, ``robot_name = "reconciled"``, in the base
station's ``target_frame``, the global map).  Every message holds the full
current set, so the console keeps the last one: that is what RViz draws from
``~/reconciled_objects`` (added = green box, removed = red box; AWCD has no
"moved" kind).

The reconciled message does not say which robots saw a change; it is recovered:
an added object's id is ``crc32(robot) << 32 | track id`` (awcd_change_store's
``reconciled_added_id``) and a removed object's id is the prior map's node id,
which every robot's own report (``/awcd_changes``) names.  Pure python: the ROS
bridge feeds :meth:`AwcdChangeSet.on_reconciled` / :meth:`on_robot` with
:func:`awcd_msg_to_dict` output.
"""

from __future__ import annotations

import threading
import time
import zlib

RECONCILED = "reconciled"  # awcd_change_store.RECONCILED
AWCD_TYPE = "khronos_msgs/msg/AwcdChanges"
ROBOT_TOPIC = "/awcd_changes"
RECONCILED_SUFFIX = "/awcd_base_station/reconciled_changes"
CLEAR_SUFFIX = "/awcd_base_station/clear_robots"
HUB_TOPIC = "awcd/changes"


def namespace_crc(name: str) -> int:
    """awcd_change_store._namespace_crc."""
    return zlib.crc32(name.encode()) & 0x7FFFFFFF


def _xyz(p) -> list[float]:
    return [float(p.x), float(p.y), float(p.z)]


def _stamp(s) -> float:
    return float(s.sec) + float(s.nanosec) * 1e-9


def _obj(o) -> dict:
    q = o.bbox_orientation
    return {
        "id": int(o.id),
        "label": str(o.name or ""),
        "center": _xyz(o.bbox_center),
        "dims": _xyz(o.bbox_dimensions),
        "quat": [float(q.x), float(q.y), float(q.z), float(q.w)],
        "stamp": _stamp(o.stamp),
        "change_confidence": float(o.change_confidence),
        "confidence": float(o.confidence),
        "frames": int(o.num_frames_observed),
    }


def awcd_msg_to_dict(msg) -> dict:
    """``khronos_msgs/AwcdChanges`` -> plain dict."""
    return {
        "robot": str(msg.robot_name),
        "frame_id": str(msg.header.frame_id),
        "stamp": _stamp(msg.header.stamp),
        "added": [_obj(o) for o in msg.added_objects],
        "removed": [_obj(o) for o in msg.removed_objects],
    }


class AwcdChangeSet:
    """The last reconciled set plus each robot's own report (for attribution)."""

    def __init__(self, clock=time.time):
        self.clock = clock
        self._lock = threading.Lock()
        self._reconciled: dict | None = None
        self._t: float | None = None
        self._topic: str | None = None
        self._robots: dict[str, dict] = {}  # robot -> {t, added_ids, removed_ids}
        self.error: str | None = None  # why there is no subscription (type missing)

    # ------------------------------------------------------------- feeding
    def on_reconciled(self, d: dict, topic: str | None = None) -> None:
        with self._lock:
            self._reconciled = d
            self._t = self.clock()
            if topic:
                self._topic = topic

    def on_robot(self, d: dict) -> None:
        robot = d.get("robot") or ""
        if not robot or robot == RECONCILED:
            return
        with self._lock:
            self._robots[robot] = {
                "t": self.clock(),
                "added": len(d.get("added") or []),
                "removed": len(d.get("removed") or []),
                "removed_ids": {int(o["id"]) for o in d.get("removed") or []},
                "added_ids": {int(o["id"]) for o in d.get("added") or []},
            }

    def set_topic(self, topic: str) -> None:
        with self._lock:
            self._topic = topic

    def forget(self, robots: list[str] | None = None) -> None:
        """After a clear at the base station: drop these robots' (all) reports.
        The base station republishes its reconciled set on its own."""
        with self._lock:
            for r in list(self._robots) if not robots else robots:
                self._robots.pop(r, None)

    # ------------------------------------------------------------- reading
    def _who(self, kind: str, oid: int, known: list[str]) -> list[str]:
        if kind == "added":
            crc = oid >> 32
            names = [r for r in known if namespace_crc(r) == crc]
            # the robots' own reports name the low 32 bits as their track id
            low = oid & 0xFFFFFFFF
            more = [
                r
                for r, s in self._robots.items()
                if r in names and low in s["added_ids"]
            ]
            return more or names
        return sorted(r for r, s in self._robots.items() if oid in s["removed_ids"])

    def state(self, robots: list[str] | None = None) -> dict:
        """``{available, topic, t, frame_id, changes: [...], robots: {...}}``.

        ``robots``: names known elsewhere (fleet roster) for the added-id
        attribution.  ``changes`` are sorted removed first, then by time."""
        with self._lock:
            rec = self._reconciled
            known = sorted(set(robots or []) | set(self._robots))
            changes = []
            if rec:
                for kind in ("removed", "added"):
                    for o in rec.get(kind) or []:
                        changes.append(
                            {
                                **o,
                                "key": f"{kind[0]}{o['id']}",
                                "kind": kind,
                                # added ids are crc(robot) << 32 | track id
                                # (past JS's 2^53): the track id is shown
                                "short_id": int(o["id"]) & 0xFFFFFFFF
                                if kind == "added"
                                else int(o["id"]),
                                "robots": self._who(kind, int(o["id"]), known),
                            }
                        )
            changes.sort(key=lambda c: (c["kind"] != "removed", c["stamp"], c["id"]))
            return {
                "available": self.error is None,
                "error": self.error,
                "topic": self._topic,
                "t": self._t,
                "frame_id": rec.get("frame_id") if rec else None,
                "changes": changes,
                "counts": {
                    "added": sum(c["kind"] == "added" for c in changes),
                    "removed": sum(c["kind"] == "removed" for c in changes),
                },
                "robots": {
                    r: {"t": s["t"], "added": s["added"], "removed": s["removed"]}
                    for r, s in sorted(self._robots.items())
                },
            }
