"""Penn quad: wire protocol, link against the mock quad, goals/captures/SAM3,
images to the model, and the chat tools (API.md §16). No network, no GPU."""

import json
import math
import threading
import time
from types import SimpleNamespace as NS

import pytest

from adt4_console import chat as chat_mod
from adt4_console.quad import protocol

from .conftest import make_client
from .synthetic import build_run

ANCHOR = {"epsg": 32618, "yaw": -0.5, "t": [482979.48, 4421319.72], "z0": 0.0}
PNG_1PX = "data:image/jpeg;base64,/9j/4AAQ"


def _ports(n):
    zmq = pytest.importorskip("zmq")
    out = []
    for _ in range(n):
        with zmq.Context.instance().socket(zmq.PULL) as s:
            out.append(s.bind_to_random_port("tcp://127.0.0.1"))
    return out


@pytest.fixture
def mock_quad():
    """A mock Penn quad (real JSON API) on two free ports, takeoff at map (0, 0)."""
    pytest.importorskip("zmq")
    from adt4_console.geo import GeoAnchor
    from adt4_console.quad import mock_server

    pq, pc = _ports(2)
    quad = mock_server.MockQuad(
        GeoAnchor(**ANCHOR), None, [], start=(0.0, 0.0), speed=40.0
    )
    stop = threading.Event()
    th = threading.Thread(
        target=mock_server.serve,
        args=(quad, f"tcp://127.0.0.1:{pq}", f"tcp://127.0.0.1:{pc}", stop),
        daemon=True,
    )
    th.start()
    time.sleep(0.2)
    yield NS(quad=quad, cmd=f"tcp://127.0.0.1:{pq}", image=f"tcp://127.0.0.1:{pc}")
    stop.set()
    th.join(timeout=2)


def _link(mock, origin=(0.0, 0.0)):
    from adt4_console.geo import GeoAnchor
    from adt4_console.quad.link import QuadLink

    return QuadLink(
        mock.cmd,
        mock.image,
        protocol.QuadFrame(GeoAnchor(**ANCHOR), origin),
        timeout_s=3.0,
    )


# ----------------------------------------------------------------- protocol
def test_protocol_requests_and_arrival_shapes():
    assert protocol.task_request(1, 2, "tcp://h:1", "r") == {
        "op": "task", "x": 1.0, "y": 2.0, "reply_to": "tcp://h:1", "req_id": "r"
    }  # fmt: skip
    assert protocol.capture_request("e", "r", require_gps=True)["require_gps"] is True
    assert "require_gps" not in protocol.capture_request("e", "r")
    for m in (
        {"event": "arrived"},
        {"type": "ARRIVED"},
        {"state": "arrived"},
        {"arrived": True},
    ):
        assert protocol.is_arrival(m)
    assert not protocol.is_arrival({"state": "search", "waiting_arrival": True})
    assert protocol.heading_to_enu_yaw(0.0) == pytest.approx(math.pi / 2)  # north
    assert protocol.heading_to_enu_yaw(90.0) == pytest.approx(0.0)  # east
    meta, jpeg = protocol.decode([b'{"op_status": "ok"}', b"\xff\xd8"])
    assert protocol.ok(meta) and jpeg == b"\xff\xd8"


def test_frame_follows_a_lazy_anchor():
    from adt4_console.geo import GeoAnchor

    holder = {"a": None}
    f = protocol.QuadFrame(lambda: holder["a"])
    assert f.anchor is None and f.map_to_quad(3.0, 4.0) == (3.0, 4.0)
    holder["a"] = GeoAnchor(**ANCHOR)
    e, n = holder["a"].map_to_utm(0.0, 0.0)
    f.set_origin(e, n, "test")
    assert f.map_to_quad(0.0, 0.0) == pytest.approx((0.0, 0.0), abs=1e-6)
    assert f.quad_to_map(*f.map_to_quad(7.0, -2.0)) == pytest.approx((7.0, -2.0))


# --------------------------------------------------------------------- link
def test_link_status_capture_task_and_arrived_event(mock_quad):
    from adt4_console.geo import GeoAnchor

    a = GeoAnchor(**ANCHOR)
    link = _link(mock_quad, a.map_to_utm(0.0, 0.0))  # takeoff known: map (0, 0)
    events = []
    link.on_event(lambda meta, jpeg: events.append(meta))
    try:
        s = link.status()
        assert s["position_map"]["x"] == pytest.approx(0.0, abs=1e-3)
        header, jpeg = link.capture()
        assert jpeg[:2] == b"\xff\xd8"
        assert header["pose_map"]["x"] == pytest.approx(0.0, abs=0.05)
        assert header["pose_map"]["via"] == "gps" and header["pose_map"][
            "z"
        ] == pytest.approx(20.0)
        assert len(header["K"]) == 4 and header["width"] > 0
        out = link.send_waypoint(12.0, -5.0)
        assert out["ack"]["accepted"] is True
        deadline = time.time() + 5
        while not any(protocol.is_arrival(e) for e in events):
            assert time.time() < deadline, "no arrived event"
            time.sleep(0.05)
        ev = next(e for e in events if protocol.is_arrival(e))
        assert ev["req_id"] == out["req_id"]
        assert link.status()["position_map"]["x"] == pytest.approx(12.0, abs=0.05)
        header, _ = link.capture()
        assert (header["pose_map"]["x"], header["pose_map"]["y"]) == pytest.approx(
            (12.0, -5.0), abs=0.05
        )
        # heading round trip: the mock flew toward the point; the map yaw comes back
        assert header["pose_map"]["yaw"] == pytest.approx(
            math.atan2(-5.0, 12.0), abs=1e-3
        )
    finally:
        link.stop()


def test_link_calibrates_takeoff_from_gps(mock_quad):
    from adt4_console.geo import GeoAnchor

    link = _link(mock_quad)  # takeoff unknown
    try:
        link.send_waypoint(
            0.0, 0.0
        )  # the mock is at (0, 0) in the uncalibrated frame too
        mock_quad.quad.x, mock_quad.quad.y = 30.0, 10.0  # it moved since takeoff
        mock_quad.quad.goal = None
        out = link.calibrate(samples=2)
        assert out["ok"] and out["samples"] == 2
        e, n = GeoAnchor(**ANCHOR).map_to_utm(0.0, 0.0)  # the mock's takeoff
        assert out["local_utm_origin"] == pytest.approx([e, n], abs=0.05)
        assert link.status()["position_map"]["x"] == pytest.approx(30.0, abs=0.05)
    finally:
        link.stop()


def test_link_fails_fast_without_a_quad():
    pytest.importorskip("zmq")
    from adt4_console.quad.link import QuadLink, QuadUnavailable

    (p,) = _ports(1)
    link = QuadLink(
        f"tcp://127.0.0.1:{p}", None, protocol.QuadFrame(None), timeout_s=1.0
    )
    link.connect_wait_s = 0.2
    t0 = time.time()
    try:
        with pytest.raises(QuadUnavailable, match="not reachable"):
            link.status()
        assert time.time() - t0 < 2.0
    finally:
        link.stop()


# ------------------------------------------------------------------ service
class Hub:
    def __init__(self):
        self.msgs = []

    def publish(self, topic, data):
        self.msgs.append((topic, data))
        return 0

    def events(self, kind):
        return [d for t, d in self.msgs if t == "quad_event" and d["kind"] == kind]


def _wait(cond, s=5.0):
    deadline = time.time() + s
    while not cond():
        assert time.time() < deadline
        time.sleep(0.05)


def test_service_goal_arrives_captures_and_segments(mock_quad, monkeypatch, tmp_path):
    from adt4_console import sam3_client
    from adt4_console.geo import GeoAnchor
    from adt4_console.quad.service import QuadService

    seen = {}

    def fake_segment(
        jpeg, prompt, base_url, conf=None, max_detections=10, timeout_s=60.0
    ):
        seen["prompt"], seen["jpeg"] = prompt, jpeg[:2]
        return {"detections": [{"label": prompt, "score": 0.9, "box_xyxy": [300, 220, 340, 260],
                                "center_px": [320, 240], "area_px": 1600}],
                "overlay_jpeg_b64": "/9j/AAAA"}  # fmt: skip

    monkeypatch.setattr(sam3_client, "segment", fake_segment)
    hub = Hub()
    settings = NS(quad_poll_s=0.1, quad_arrive_m=0.5, quad_goal_timeout_s=30,
                  quad_images_dir=str(tmp_path), sam3_url="http://sam3")  # fmt: skip
    svc = QuadService(
        _link(mock_quad, GeoAnchor(**ANCHOR).map_to_utm(0.0, 0.0)), hub, settings
    )
    svc.start()
    try:
        out = svc.goal(
            8.0,
            6.0,
            capture=True,
            query="crate",
            label="O(3) crate",
            chat={"follow_up": True},
        )
        assert out["goal"]["status"] == "flying" and hub.events("sent")
        _wait(lambda: hub.events("captured"))
        ev = hub.events("captured")[0]
        assert ev["goal"]["status"] == "arrived" and ev["goal"]["chat"] == {
            "follow_up": True
        }
        cap = ev["capture"]
        assert cap["image_url"].endswith(".jpg") and cap["overlay_url"]
        det = cap["segment"]["detections"][0]
        assert seen == {"prompt": "crate", "jpeg": b"\xff\xd8"}
        # the image centre is straight below the quad
        assert det["map_xy_approx"] == pytest.approx([8.0, 6.0], abs=0.2)
        assert list((tmp_path / "captures").glob("*.jpg"))
        summary, images = svc.for_model(cap["id"])
        assert summary["segment"]["query"] == "crate" and images[0].startswith(
            "data:image/jpeg;base64,"
        )
        assert any(t == "quad/state" for t, _ in hub.msgs)
    finally:
        svc.stop()


def test_service_goal_times_out(mock_quad):
    from adt4_console.quad.service import QuadService

    hub = Hub()
    svc = QuadService(
        _link(mock_quad),
        hub,
        NS(quad_poll_s=0.1, quad_arrive_m=0.5, quad_goal_timeout_s=0.3),
    )
    mock_quad.quad.speed = 0.0  # never gets there
    svc.start()
    try:
        svc.goal(50.0, 0.0, capture=False)
        _wait(lambda: hub.events("failed"))
    finally:
        svc.stop()


# --------------------------------------------------------- images to model
def _tool_items():
    return [
        {"role": "user", "text": "look"},
        {"role": "assistant", "text": "", "calls": [{"id": "c1", "name": "quad_capture", "args": {}},
                                                    {"id": "c2", "name": "quad_status", "args": {}}]},
        chat_mod.tool_item({"id": "c1", "name": "quad_capture"}, {"capture_id": "q1", "_images": [PNG_1PX]}),
        chat_mod.tool_item({"id": "c2", "name": "quad_status"}, {"pose": None}),
    ]  # fmt: skip


def test_openai_images_follow_the_tool_messages():
    msgs = chat_mod.to_openai(_tool_items())
    assert [m["role"] for m in msgs] == ["user", "assistant", "tool", "tool", "user"]
    assert "_images" not in msgs[2]["content"] and "base64" not in msgs[2]["content"]
    parts = msgs[4]["content"]
    assert parts[1] == {"type": "image_url", "image_url": {"url": PNG_1PX}}


def test_anthropic_images_ride_in_the_tool_result():
    msgs = chat_mod.to_anthropic(_tool_items())
    res = msgs[2]["content"][0]
    assert res["type"] == "tool_result" and res["content"][1]["type"] == "image"
    assert res["content"][1]["source"] == {
        "type": "base64",
        "media_type": "image/jpeg",
        "data": "/9j/4AAQ",
    }
    assert msgs[2]["content"][1]["content"] == json.dumps({"pose": None})


def test_responses_images_and_user_attachments():
    items = _tool_items()
    out = chat_mod.to_responses(items)
    assert out[-1]["content"][1] == {"type": "input_image", "image_url": PNG_1PX}
    user = chat_mod.to_responses(
        [{"role": "user", "text": "here", "images": [PNG_1PX]}]
    )
    assert user[0]["content"][1]["type"] == "input_image"
    assert all(
        "images" not in it
        for it in chat_mod.strip_images(items + [{"role": "user", "images": [1]}])
    )


# --------------------------------------------------------------------- chat
class FakeCompletions:
    def __init__(self, script):
        self.script, self.calls = list(script), []

    def create(self, **kw):
        self.calls.append(kw)
        return NS(model="gpt-4.1-mini-2025-04-14", usage=NS(prompt_tokens=10, completion_tokens=5),
                  choices=[NS(message=self.script.pop(0))])  # fmt: skip


def _call(cid, name, args):
    return NS(id=cid, function=NS(name=name, arguments=json.dumps(args)))


@pytest.fixture
def quad_chat(settings, tmp_path, monkeypatch, mock_quad):
    monkeypatch.setenv("ADT4_OPENAI_API_KEY", "sk-test")
    settings.llm = True
    settings.llm_usage_path = str(tmp_path / "usage.json")
    settings.maps = {"syn": str(build_run(tmp_path / "run"))}
    settings.quad_cmd_uri, settings.quad_image_uri = mock_quad.cmd, mock_quad.image
    settings.quad_poll_s = 0.1
    client, svc = make_client(settings)
    with client:
        yield client, svc


def _chat(client, message, **kw):
    r = client.post("/api/chat", json={"message": message, **kw})
    return [json.loads(line) for line in r.text.splitlines()]


def _script(svc, script):
    fake = NS(chat=NS(completions=FakeCompletions(script)))
    svc.chat._client, svc.chat._client_provider = fake, "openai"
    return fake.chat.completions


def test_chat_proposes_a_quad_goal_card(quad_chat):
    client, svc = quad_chat
    calls = _script(svc, [
        NS(content=None, tool_calls=[_call("1", "propose_quad_goal",
                                           {"x": 4.0, "y": 2.0, "query": "car", "reason": "look"})]),
        NS(content="Sent for approval.", tool_calls=None),
    ])  # fmt: skip
    out = _chat(client, "fly the quad over (4, 2) and look for a car", src="syn")
    card = next(o for o in out if o["type"] == "quad_goal")
    assert (card["x"], card["y"], card["capture"], card["query"]) == (
        4.0,
        2.0,
        True,
        "car",
    )
    tools = [t["function"]["name"] for t in calls.calls[0]["tools"]]
    assert {
        "propose_quad_goal",
        "quad_capture",
        "segment_quad_image",
        "quad_status",
    } <= set(tools)
    assert "Penn's quadrotor" in calls.calls[0]["messages"][0]["content"]


def test_chat_capture_shows_the_image_to_the_model(quad_chat):
    client, svc = quad_chat
    calls = _script(svc, [
        NS(content=None, tool_calls=[_call("1", "quad_capture", {})]),
        NS(content="I see grass.", tool_calls=None),
    ])  # fmt: skip
    out = _chat(client, "what is under the quad?", src="syn")
    imgs = next(o for o in out if o["type"] == "images")
    url = imgs["items"][0]["images"][0]
    assert (
        url.startswith("api/quad/captures/")
        and client.get("/" + url).content[:2] == b"\xff\xd8"
    )
    second = calls.calls[1]["messages"]
    assert second[-2]["role"] == "tool" and "base64" not in second[-2]["content"]
    assert second[-1]["content"][1]["type"] == "image_url"
    transcript = next(o for o in out if o["type"] == "transcript")["items"]
    assert "base64" not in json.dumps(transcript)


def test_chat_follow_up_attaches_the_capture(quad_chat):
    client, svc = quad_chat
    c = svc.quad_svc.capture()
    calls = _script(svc, [NS(content="A parking lot.", tool_calls=None)])
    out = _chat(
        client,
        "[quad] arrived over O(3): what do you see?",
        src="syn",
        attach={"quad_capture": c["id"]},
    )
    assert any(o["type"] == "text" for o in out)
    user = calls.calls[0]["messages"][-1]
    assert user["role"] == "user" and user["content"][1]["type"] == "image_url"
    assert f"[quad capture {c['id']}]" in user["content"][0]["text"]


def test_quad_routes(quad_chat):
    client, svc = quad_chat
    r = client.post("/api/quad/goal", json={"x": 3.0, "y": 0.0, "capture": False})
    assert r.status_code == 200, r.text
    assert r.json()["goal"]["status"] == "flying"
    r = client.post("/api/quad/image", json={})
    assert r.status_code == 200 and r.json()["jpeg"].startswith("data:image/jpeg")
    st = client.get("/api/quad").json()
    assert st["configured"] and st["goal"]["x"] == 3.0
    assert (
        client.get("/api/quad/captures").json()["captures"][0]["id"] == r.json()["id"]
    )
    assert client.get("/api/quad/captures/nope.jpg").status_code == 404
