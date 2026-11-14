"""Live image-store mirror (fleetlink C6) in the console: ``/api/live`` reports
the per-robot mirror (frames, bytes, backlog, path under ``live_store_root``)."""

import dataclasses

import numpy as np
import pytest

from .conftest import make_client, needs_fleetlink

pytest.importorskip("hydra_python.image_store")
pytest.importorskip("zstandard")

pytestmark = needs_fleetlink
cv2 = pytest.importorskip("cv2")


def _robot_store(root, n):
    from hydra_python.image_store import FrameKind, ImageStore, make_node_id

    src = root.parent / "src"
    src.mkdir(parents=True)
    w = ImageStore.open_writer(root, robot_id=1)
    sid = w.add_sensor("camera", 32, 24, 30.0, 30.0, 16, 12, 0.001, np.eye(4))
    ids = []
    for i in range(n):
        ts = 1_000_000_000 + i * 100_000_000
        rp, dp = src / f"{ts}_rgb.jpg", src / f"{ts}_depth.png"
        cv2.imwrite(str(rp), np.full((24, 32, 3), i * 20, np.uint8))
        cv2.imwrite(str(dp), np.full((24, 32), 1000 + i, np.uint16))
        ids.append(
            # anchored at its own agent node (identity offset): an own keyframe, which
            # fleetlink's default keyframes mode sends
            w.add_frame_ref(
                sid,
                ts,
                kind=FrameKind.KEYFRAME,
                rgb=rp,
                depth=dp,
                link="copy",
                anchor_node_id=make_node_id("a", i),
                anchor_T_body=np.eye(4),
            )
        )
    w.close()
    return ids


def test_live_reports_store_mirror(settings, tmp_path):
    from fleetlink.agent import AgentCore
    from fleetlink.receiver import Receiver
    from fleetlink.store_mirror import StoreMirror
    from fleetlink.transport import LinkEmulator, VirtualClock

    st = dataclasses.replace(settings, live_store_root=str(tmp_path / "live"))
    ids = _robot_store(tmp_path / "robot" / "image_store", 3)
    clock = VirtualClock()
    link = LinkEmulator(clock=clock, bandwidth=None, latency=0.01)
    core = AgentCore(
        "hamilton", link.robot,
        {"store_path": str(tmp_path / "robot" / "image_store"), "store_poll_s": 0.2, "store_send": True},
        clock=clock,
    )  # fmt: skip
    rx = Receiver(
        link.base, clock=clock, threaded=False,
        store_mirror=StoreMirror(tmp_path / "live", clock=clock, threaded=False),
    )  # fmt: skip
    client, svc = make_client(st, receiver=rx)
    with client:
        assert svc.live.store_root == str(tmp_path / "live")
        for _ in range(80):
            clock.advance(0.05)
            core.tick()
            link.advance()
            rx.tick()
        live = client.get("/api/live").json()
        mirror = live["store"]["robots"]["hamilton"]
        assert mirror["frames"] == 3 and mirror["watermark"] == ids[-1]
        assert mirror["backlog"] == 0 and mirror["bytes"] > 0
        assert mirror["path"].startswith(str(tmp_path / "live" / "hamilton"))
        assert live["robots"]["hamilton"]["store"]["frames"] == 3
        assert live["robots"]["hamilton"]["budgets"]["C6"] == 150_000
        r = client.post("/api/live/hamilton/budget", json={"cls": "C6", "bps": 1000})
        assert r.status_code == 200


def test_store_root_default_and_disabled(settings, monkeypatch):
    """The mirror goes under the CURRENT run's output dir, never a shared
    <output_root>/live that holds earlier runs (other odometry frames)."""
    from adt4_console.live_service import LiveService

    monkeypatch.setenv("ADT4_OUTPUT_DIR", "/runs/nov13_v8")
    assert LiveService(settings).store_root == "/runs/nov13_v8/live"
    monkeypatch.delenv("ADT4_OUTPUT_DIR")
    root = LiveService(settings).store_root
    assert "/out/console_" in root and root.endswith("/live")
    assert root != settings.output_root + "/live"
    off = dataclasses.replace(settings, live_store_root="")
    assert LiveService(off).store_root is None


def test_live_state_lists_current_stores_and_relocalization(
    settings, monkeypatch, tmp_path
):
    """console_live.json: the SAM3 server's only source of live images."""
    import json

    from adt4_console.live_service import LiveService

    monkeypatch.setenv("ADT4_OUTPUT_DIR", str(tmp_path / "run"))
    fa = {
        "ok": True,
        "parent": "map",
        "child": "hamilton/map",
        "T": [1, 2, 0, 1, 0, 0, 0],
    }
    health = {
        "nodes": [
            {"nickname": "hydra_multi", "notes": "7 / 9 valid interrobot loop closures"}
        ]
    }

    class Rx:
        def robots(self):
            return ["hamilton", "euclid"]

        def store_stats(self, robot=None):
            return {
                "hamilton": {
                    "store_id": "abc123",
                    "path": str(tmp_path / "run/live/hamilton/abc123/image_store"),
                }
            }

        def state(self, robot):
            if robot == "hamilton":
                return {"frame_align": fa, "health": health, "reloc": {"prior_lcs": 3}}
            return {
                "frame_align": {"ok": False, "parent": "map", "child": "euclid/map"}
            }

        def link(self, robot):
            return {}

        store = object()
        paths = grid = lambda self, r: None

    live = LiveService(settings, receiver=Rx())
    path = live.write_live_state()
    assert path == str(tmp_path / "out" / "console_live.json")
    st = json.loads(open(path).read())
    assert st["live_store_root"] == str(tmp_path / "run" / "live")
    h, e = st["robots"]["hamilton"], st["robots"]["euclid"]
    assert h["store_id"] == "abc123" and h["path"].endswith("abc123/image_store")
    assert h["relocalized"] is True and h["frame_align"]["T"] == [1, 2, 0, 1, 0, 0, 0]
    assert e["store_id"] is None and e["relocalized"] is False
    # a clean stop removes our own state file
    live._remove_live_state()
    assert not (tmp_path / "out" / "console_live.json").exists()
    off = LiveService(dataclasses.replace(settings, live_state_path=""), receiver=Rx())
    assert off.write_live_state() is None


def test_store_send_route_switches_the_mirror(settings, tmp_path):
    """Off by default; POST /api/live/{r}/store_send turns it on and off."""
    from fleetlink.agent import AgentCore
    from fleetlink.receiver import Receiver
    from fleetlink.store_mirror import StoreMirror
    from fleetlink.transport import LinkEmulator, VirtualClock

    st = dataclasses.replace(settings, live_store_root=str(tmp_path / "live"))
    ids = _robot_store(tmp_path / "robot" / "image_store", 3)
    clock = VirtualClock()
    link = LinkEmulator(clock=clock, bandwidth=None, latency=0.01)
    core = AgentCore(
        "hamilton", link.robot,
        {"store_path": str(tmp_path / "robot" / "image_store"), "store_poll_s": 0.2},
        clock=clock,
    )  # fmt: skip
    rx = Receiver(
        link.base, clock=clock, threaded=False,
        store_mirror=StoreMirror(tmp_path / "live", clock=clock, threaded=False),
    )  # fmt: skip

    def run(n=80):
        for _ in range(n):
            clock.advance(0.05)
            core.tick()
            link.advance()
            rx.tick()

    client, _svc = make_client(st, receiver=rx)
    with client:
        run()
        live = client.get("/api/live").json()["robots"]["hamilton"]
        assert live["store_send"] == {"robot": False, "wanted": None}
        assert live["store"]["frames"] == 0 and live["store"]["backlog"] == 3

        r = client.post("/api/live/hamilton/store_send", json={"enabled": True})
        assert r.status_code == 200 and r.json()["store_send"]["wanted"] is True
        run()
        live = client.get("/api/live").json()["robots"]["hamilton"]
        assert live["store_send"] == {"robot": True, "wanted": True}
        assert live["store"]["frames"] == 3 and live["store"]["watermark"] == ids[-1]

        client.post("/api/live/hamilton/store_send", json={"enabled": False})
        run(20)
        assert core.store_send is False
        assert (
            client.get("/api/live").json()["robots"]["hamilton"]["store_send"]["robot"]
            is False
        )
