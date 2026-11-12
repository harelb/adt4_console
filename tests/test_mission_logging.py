"""Mission logging (A2/A5): eventlog, hooks, sessions, the relocalization gate, missions."""

from __future__ import annotations

import json
import pathlib
import time
from types import SimpleNamespace as NS

import pytest

from adt4_console import eventlog, logbook
from adt4_console.eventtap import HubTap, match
from adt4_console.goal_tracker import GoalTracker
from adt4_console.logbook import Conflict, GateClosed, Logbook, goal_symbols

from .conftest import make_client, needs_fleet_core


@pytest.fixture
def evroot(tmp_path, monkeypatch):
    monkeypatch.delenv("ADT4_RUN_DIR", raising=False)
    eventlog.reset()
    eventlog.enable(True, fallback_root=tmp_path / "base")
    yield tmp_path / "base"
    eventlog.reset()


def events(root: pathlib.Path, source: str) -> list[dict]:
    """``source``'s events (in the current event root: a logbook session moves it
    to the base experiment dir)."""
    eventlog.flush()
    root = eventlog.root() or root
    p = root / "events" / f"{source}.jsonl"
    return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []


def types(root, source):
    return [e["type"] for e in events(root, source)]


# ------------------------------------------------------------------ eventlog
def test_envelope(evroot):
    eventlog.set_ids(session_id="s1", mission_id="m1")
    eventlog.emit("unit", "x.y", {"a": 1}, goal_id="g1", plan_id="p1", robot="euclid")
    eventlog.emit("unit", "x.z")
    ev = events(evroot, "unit")
    assert [e["seq"] for e in ev] == [1, 2]
    e = ev[0]
    for k in ("v", "t_wall", "t_mono", "t_ros", "machine", "robot", "run_id", "session_id", "mission_id",
              "goal_id", "plan_id", "source", "type", "seq", "payload"):  # fmt: skip
        assert k in e, k
    assert (e["v"], e["source"], e["type"], e["goal_id"], e["plan_id"], e["robot"]) == (
        1, "unit", "x.y", "g1", "p1", "euclid")  # fmt: skip
    assert (e["session_id"], e["mission_id"], e["payload"]) == ("s1", "m1", {"a": 1})


def test_run_dir_wins(tmp_path, monkeypatch, evroot):
    monkeypatch.setenv("ADT4_RUN_DIR", str(tmp_path / "run"))
    eventlog.emit("unit", "a")
    eventlog.flush()
    assert (tmp_path / "run" / "events" / "unit.jsonl").is_file()
    assert not (evroot / "events" / "unit.jsonl").exists()


def test_off_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("ADT4_RUN_DIR", raising=False)
    eventlog.reset()
    rec = eventlog.emit("unit", "a", {"x": 1})
    assert rec["type"] == "a"  # stamped, but nowhere to go
    assert eventlog.root() is None


def test_redaction_and_no_env(evroot, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-supersecretvalue1234567890")
    eventlog.emit("unit", "a", {
        "api_key": "abc", "headers": {"Authorization": "Bearer xyz123456789"}, "password": "pw",
        "auth_token": "t", "max_tokens": 4000, "in_tok": 12, "env": dict(__import__("os").environ),
        "note": "key sk-supersecretvalue1234567890 leaked",
    })  # fmt: skip
    p = events(evroot, "unit")[0]["payload"]
    assert p["api_key"] == p["password"] == p["auth_token"] == "[redacted]"
    assert p["headers"]["Authorization"] == "[redacted]"
    assert p["max_tokens"] == 4000 and p["in_tok"] == 12
    assert p["env"] == "[env omitted]"
    raw = (evroot / "events" / "unit.jsonl").read_text()
    assert "sk-supersecret" not in raw and "xyz123456789" not in raw


def test_rotation(evroot):
    eventlog.enable(True, rotate_bytes=6000)
    for i in range(60):
        eventlog.emit("rot", "a", {"i": i, "pad": "x" * 100})
    eventlog.flush()
    files = sorted(p.name for p in (evroot / "events").glob("rot*.jsonl"))
    assert "rot.1.jsonl" in files and "rot.2.jsonl" in files
    n = sum(len(p.read_text().splitlines()) for p in (evroot / "events").glob("rot*.jsonl"))
    assert n == 60


def test_large_blobs_go_to_files(evroot):
    big = "data:image/jpeg;base64," + "A" * (eventlog.BLOB_BYTES + 12)
    eventlog.emit("unit", "a", {"img": big})
    ref = events(evroot, "unit")[0]["payload"]["img"]
    assert set(ref) >= {"$file"} and (evroot / ref["$file"]).read_bytes()[:3] == b"\x00\x00\x00"


# ------------------------------------------------------------------ goal tracker
def test_goal_tracker_stage_and_outcome(evroot):
    t = [1000.0]
    gt = GoalTracker(clock=lambda: t[0])
    gid = gt.goal_sent("euclid", kind="pddl", summary="(visited-object o1)")
    t[0] += 1
    gt.on_plan("euclid", {"plan_id": "p1", "actions": [{"type": "FOLLOW"}]})
    t[0] += 1
    gt.on_result("euclid", {"plan_id": "p1", "action_index": 0, "action_type": "FOLLOW", "status": "SUCCESS",
                            "detail": "", "t": t[0]})  # fmt: skip
    ev = events(evroot, "goal_tracker")
    stages = [(e["payload"]["robot"], e["payload"]["from"], e["payload"]["to"]) for e in ev if e["type"] == "goal.stage"]
    assert (None, None, "planning") in stages
    assert ("euclid", "planned", "executing") in stages or ("euclid", "waiting", "planned") in stages
    outs = [e for e in ev if e["type"] == "goal.outcome"]
    assert outs and outs[-1]["payload"]["result"] == "done" and outs[-1]["goal_id"] == gid
    assert all(e["goal_id"] == gid for e in ev)


# ------------------------------------------------------------------ logbook
def _lb(tmp_path, state=None, **kw):
    s = NS(output_root=str(tmp_path / "out"))
    lb = Logbook(s, None, live_state=lambda r: (state or {}).get(r), persist=True,
                 state_path=str(tmp_path / "ptr.json"), **kw)  # fmt: skip
    return lb


RELOC_PLAN = {"experiment": "exp1", "robots": ["euclid", "hamilton"], "mode": "relocalize", "prior_path": "/p",
              "base_station": "willow"}  # fmt: skip


def test_reloc_gate_state_machine(tmp_path, evroot):
    st = {"euclid": {"health": {"nodes": [{"nickname": "hydra_multi", "notes": "3 / 5 valid interrobot loop closures"}]},
                     "frame_align": {"ok": True, "T": [1, 2, 0, 1, 0, 0, 0]}}}  # fmt: skip
    lb = _lb(tmp_path, st)
    sid = lb.new_session(RELOC_PLAN)
    assert sid.startswith("s") and lb.gate()["ok"] is False
    assert set(lb.gate()["blocking"]) == {"euclid", "hamilton"}
    with pytest.raises(GateClosed):
        lb.start_mission()
    with pytest.raises(Conflict):
        lb.reloc_confirm("euclid")  # not relocalizing yet
    lb.reloc_start("euclid", "harel")
    r = lb.reloc_confirm("euclid", "harel", note="ok")
    assert r["state"] == "relocalized"
    with pytest.raises(ValueError):
        lb.reloc_override("hamilton", "  ")
    lb.reloc_override("hamilton", "radio down, start without it")
    assert lb.gate()["ok"]
    m = lb.start_mission("first")
    assert m["mission_id"].startswith("m") and m["session_id"] == sid
    base = tmp_path / "out" / "exp1"
    rel = json.loads((base / "sessions" / sid / "reloc.json").read_text())
    assert rel["robots"]["euclid"]["state"] == "relocalized"
    assert (base / "missions" / m["mission_id"] / "reloc.json").is_file()
    assert json.loads((base / "missions" / m["mission_id"] / "mission.json").read_text())["state"] == "open"
    ev = {e["type"]: e for e in events(evroot, "console")}
    conf = ev["reloc.confirmed"]["payload"]
    assert conf["hydra_accepted"] == 3 and conf["hydra_total"] == 5 and conf["map_T_odom"][0] == 1
    assert ev["reloc.override"]["payload"]["reason"].startswith("radio")
    assert {"session.start", "op.launch", "reloc.started", "mission.start"} <= set(ev)


def test_mapping_mode_not_required(tmp_path, evroot):
    lb = _lb(tmp_path)
    lb.new_session({**RELOC_PLAN, "mode": "mapping"})
    assert lb.gate()["ok"]
    assert types(evroot, "console").count("reloc.not_required") == 2


def test_evidence_ticker_and_failed(tmp_path, evroot):
    t = [100.0]
    lb = _lb(tmp_path, {}, clock=lambda: t[0])
    lb.new_session(RELOC_PLAN)
    lb.reloc_start("euclid")
    lb.tick()
    t[0] += 3
    lb.tick()  # < 10 s: no second sample
    t[0] += 10
    lb.tick()
    assert types(evroot, "console").count("reloc.evidence") == 2
    lb.reloc_fail("euclid", note="no matches")
    assert lb.reloc["euclid"]["state"] == "failed"
    lb.reloc_start("euclid")  # retry from failed
    assert lb.reloc["euclid"]["state"] == "relocalizing"


def test_degraded_during_mission(tmp_path, evroot):
    st = {"euclid": {"frame_align": {"ok": True, "T": [0, 0, 0, 1, 0, 0, 0]}}}
    lb = _lb(tmp_path, st)
    lb.new_session({**RELOC_PLAN, "robots": ["euclid"]})
    lb.reloc_start("euclid")
    lb.reloc_confirm("euclid")
    lb.start_mission()
    st["euclid"]["frame_align"]["T"] = [5, 0, 0, 1, 0, 0, 0]
    lb.tick()
    lb.tick()
    assert types(evroot, "console").count("reloc.degraded") == 1


def test_auto_open_on_goal_sent_and_restart(tmp_path, evroot):
    lb = _lb(tmp_path)
    lb.new_session({**RELOC_PLAN, "mode": "mapping"})
    eventlog.emit("console", "goal.sent", {"robot": "euclid", "payload": {"goal": "o1"}}, goal_id="g1")
    m = lb.mission
    assert m is not None and m["start_reason"] == "first_goal" and m["goals"] == ["g1"]
    sent = [e for e in events(evroot, "console") if e["type"] == "goal.sent"][0]
    assert sent["mission_id"] == m["mission_id"]  # opened before goal.sent was stamped
    eventlog.emit("console", "goal.sent", {"robot": "euclid"}, goal_id="g2")
    assert lb.mission["mission_id"] == m["mission_id"] and lb.mission["goals"] == ["g1", "g2"]
    # a restarted console reloads the open session and mission
    lb2 = _lb(tmp_path)
    lb2.start()
    lb2.stop()
    assert lb2.mission["mission_id"] == m["mission_id"] and lb2.session["session_id"] == lb.session["session_id"]
    assert "console.restart" in types(evroot, "console")
    lb2.end_mission()
    d = tmp_path / "out" / "exp1" / "missions" / m["mission_id"] / "mission.json"
    assert json.loads(d.read_text())["end_reason"] == "operator"


def test_no_auto_open_while_gate_closed(tmp_path, evroot):
    lb = _lb(tmp_path)
    lb.new_session(RELOC_PLAN)
    eventlog.emit("console", "goal.sent", {"robot": "euclid"}, goal_id="g1")
    assert lb.mission is None


def test_session_stop_ends_mission(tmp_path, evroot):
    lb = _lb(tmp_path)
    lb.new_session({**RELOC_PLAN, "mode": "mapping"})
    lb.start_mission()
    lb.robot_stop_saved("euclid")
    assert lb.session["state"] == "open"
    lb.robot_stop_saved("hamilton")
    assert lb.session["state"] == "stopped" and lb.mission is None
    end = [e for e in events(evroot, "console") if e["type"] == "mission.end"][0]
    assert end["payload"]["reason"] == "session_stop"


def test_mission_topic_published(tmp_path, evroot):
    pubs = []
    lb = _lb(tmp_path)
    lb.ros_publish = lambda topic, t, f: pubs.append((topic, t, json.loads(f["data"])))
    assert lb.publish_mission(force=True)
    assert pubs[-1] == (logbook.MISSION_TOPIC, "std_msgs/msg/String", {"mission_id": "", "state": "closed", "session_id": ""})
    sid = lb.new_session({**RELOC_PLAN, "mode": "mapping"})
    m = lb.start_mission()
    assert pubs[-1][2] == {"mission_id": m["mission_id"], "state": "open", "session_id": sid}
    lb.end_mission()
    assert pubs[-1][2] == {"mission_id": "", "state": "closed", "session_id": sid}


def test_goal_images_copied(tmp_path, evroot):
    img = tmp_path / "img.jpg"
    img.write_bytes(b"\xff\xd8jpeg")
    lb = _lb(tmp_path, image_lookup=lambda sym: [img] if sym == "O(12)" else [])
    lb.new_session({**RELOC_PLAN, "mode": "mapping"})
    eventlog.emit("console", "goal.sent", {"robot": "euclid", "payload": {"goal": {"pddl_goal": "(visited-object o12)"}}},
                  goal_id="g7")  # fmt: skip
    lb._last_image_thread.join(5)
    files = list((tmp_path / "out" / "exp1" / "missions" / lb.mission["mission_id"] / "images" / "goals" / "g7").iterdir())
    assert [f.read_bytes() for f in files] == [b"\xff\xd8jpeg"]
    ev = [e for e in events(evroot, "console") if e["type"] == "goal.images"][0]
    assert ev["payload"]["refs"][0]["sym"] == "O(12)" and ev["goal_id"] == "g7"


def test_goal_symbols():
    assert goal_symbols({"goal": "(and (visited-object o12) (visited-place p850) (in-region hamilton r3))"}) == [
        "O(12)", "P(850)", "R(3)"]  # fmt: skip
    assert goal_symbols({"point_names_to_visit": ["O(4)", "p(5)"]}) == ["O(4)", "P(5)"]


# ------------------------------------------------------------------ hub tap
def test_hub_tap_throttles(evroot):
    t = [0.0]
    tap = HubTap(clock=lambda: t[0])
    for _ in range(5):
        tap("link/euclid", {"latency_ms": 12.0, "rtt_ms": 30.0, "clock_offset_ms": 4.0, "loss": {"C1": 0.0}})
        tap("robot/euclid/state", {"pose": {"x": 1, "y": 2, "z": 0, "yaw": 0.5}})
        t[0] += 0.3
    t[0] += 6
    tap("link/euclid", {"latency_ms": 12.0, "clock_offset_ms": 4.0})
    tap("video/euclid/front", {"x": 1})
    tap("awcd/changes", {"counts": {"added": 1}, "robots": {}, "changes": [{}]})
    tap("awcd/changes", {"counts": {"added": 1}, "robots": {}, "changes": [{}]})
    ty = types(evroot, "console")
    assert ty.count("link.stats") == 2 and ty.count("clock.offset") == 1
    assert ty.count("robot.pose") == 2  # 1 Hz: the samples at 0 s and 1.2 s
    assert ty.count("reloc.state") == 1 and ty.count("awcd.changes") == 1


def test_rest_rules():
    assert match("POST", "/api/robots/euclid/take_lease") == ("op.take_lease", {"robot": "euclid"})
    assert match("POST", "/api/robots/euclid/executor/pause")[0] == "op.pause"
    assert match("POST", "/api/plan/scene_repair/r1/decline")[1] == {"rid": "r1", "decision": "decline"}
    assert match("POST", "/api/robots/euclid/teleop/cmd_vel") is None
    assert match("GET", "/api/robots/euclid/take_lease") is None


# ------------------------------------------------------------------ chat
class _Usage:
    prompt_tokens = 100
    completion_tokens = 20
    completion_tokens_details = NS(reasoning_tokens=5)


def test_chat_logs_turn_tool_calls_and_llm(settings, evroot, monkeypatch):
    from adt4_console.chat import ChatService

    settings.llm = True
    monkeypatch.setenv("OPENAI_API_KEY", "sk-testkeytestkeytestkey")
    settings.llm_usage_path = str(evroot.parent / "usage.json")
    replies = [
        NS(model="gpt-4.1-mini", usage=_Usage(), choices=[NS(message=NS(content="", tool_calls=[
            NS(id="c1", function=NS(name="propose_goal", arguments=json.dumps(
                {"kind": "goto", "points": ["O(1)"], "robot": "euclid"})))]))]),
        NS(model="gpt-4.1-mini", usage=_Usage(), choices=[NS(message=NS(content="Sent.", tool_calls=[]))]),
    ]  # fmt: skip
    client = NS(chat=NS(completions=NS(create=lambda **kw: replies.pop(0))))
    graph = NS(list=lambda: [])
    chat = ChatService(settings, graph)
    chat._client, chat._client_provider = client, "openai"  # noqa: SLF001
    monkeypatch.setattr(chat, "llm_client", lambda: client)
    out = list(chat.stream("go to the chair", robot="euclid", model="gpt-4.1-mini"))
    assert any(e["type"] == "goal" for e in out)
    ev = events(evroot, "chat")
    ty = [e["type"] for e in ev]
    assert ty[0] == "chat.user" and ty[-1] == "chat.assistant"
    assert {"llm.call", "chat.tool_call", "chat.tool_result", "goal.proposed"} <= set(ty)
    llm = [e["payload"] for e in ev if e["type"] == "llm.call"][0]
    assert llm["provider"] == "openai" and llm["ok"] and llm["in_tok"] == 100 and llm["out_tok"] == 20
    assert llm["reasoning_tok"] == 5 and llm["cost_usd"] > 0 and llm["latency_ms"] >= 0
    assert "messages" not in llm  # no prompt bodies by default
    turn = ev[0]["payload"]["turn_id"]
    assert all(e["payload"].get("turn_id") == turn for e in ev)
    assert [e for e in ev if e["type"] == "chat.assistant"][0]["payload"]["text"] == "Sent."
    assert "sk-testkey" not in (evroot / "events" / "chat.jsonl").read_text()


def test_llm_call_error_logged(settings, evroot):
    from adt4_console.chat import ChatService

    chat = ChatService(settings, NS(list=lambda: []))

    def boom(**kw):
        raise RuntimeError("down")

    with pytest.raises(RuntimeError):
        chat._llm_call("anthropic", None, boom, model="claude-x")  # noqa: SLF001
    p = events(evroot, "chat")[0]["payload"]
    assert p["ok"] is False and "down" in p["error"] and p["model"] == "claude-x"


# ------------------------------------------------------------------ sam3
def test_sam3_query_commit_discard(evroot, monkeypatch):
    from adt4_console import sam3_client

    uri = "data:image/jpeg;base64,/9j/AA=="

    def post(base, path, payload, timeout):
        if path == "/query":
            return {"pending_id": "pq", "candidates": [{"index": 0, "label": "chair", "score": 0.9, "image_uri": uri}]}
        if path == "/commit":
            return {"symbol": "O(77)", "created": True}
        return {}

    monkeypatch.setattr(sam3_client, "_post", post)
    sam3_client.query("chair", "http://x")
    sam3_client.commit("pq", 0, "http://x")
    sam3_client.discard("pq", "http://x")
    ev = events(evroot, "sam3")
    ty = [e["type"] for e in ev]
    assert ty == ["sam3.query", "sam3.candidates", "sam3.commit", "sam3.discard"]
    assert ev[0]["payload"]["n_candidates"] == 1 and ev[0]["payload"]["pending_id"] == "pq"
    crop = ev[1]["payload"]["list"][0]["crop"]
    assert (eventlog.root() / crop["$file"]).is_file()
    assert ev[2]["payload"]["object_id"] == "O(77)"


# ------------------------------------------------------------------ API
def test_logbook_api_gate(settings, evroot):
    settings.mission_log = True
    client, svc = make_client(settings)
    with client:
        assert client.get("/api/logbook").json()["gate"]["ok"] is True  # no session
        svc.logbook.new_session(RELOC_PLAN)
        r = client.post("/api/logbook/mission/start", json={"name": "a"})
        assert r.status_code == 409 and "euclid" in r.json()["detail"]
        assert client.post("/api/logbook/reloc/euclid/confirm", json={}).status_code == 409
        assert client.post("/api/logbook/reloc/nobody/start", json={}).status_code == 404
        assert client.post("/api/logbook/reloc/euclid/start", json={"operator": "op"}).status_code == 200
        assert client.post("/api/logbook/reloc/euclid/confirm", json={"note": "seen"}).status_code == 200
        assert client.post("/api/logbook/reloc/hamilton/override", json={"reason": ""}).status_code == 400
        assert client.post("/api/logbook/reloc/hamilton/override", json={"reason": "skip"}).status_code == 200
        v = client.get("/api/logbook").json()
        assert v["gate"]["ok"] and v["reloc"]["euclid"]["state"] == "relocalized"
        assert "evidence" in v["reloc"]["euclid"]
        r = client.post("/api/logbook/mission/start", json={"name": "a"})
        assert r.status_code == 200 and r.json()["mission"]["name"] == "a"
        assert client.post("/api/logbook/mission/start", json={}).status_code == 409  # already open
        r = client.post("/api/logbook/mission/end", json={})
        assert r.status_code == 200 and r.json()["mission"] is None and r.json()["ended"]["end_reason"] == "operator"
        assert client.post("/api/logbook/mission/end", json={}).status_code == 409


def test_rest_tap_logs_operator_actions(settings, evroot):
    client, svc = make_client(settings)
    with client:
        client.post("/api/plan/scene_repair/r9/decline", json={"resume": False})
        client.post("/api/robots/euclid/take_lease")
        client.post("/api/robots/euclid/goal/goto", json={"points": ["O(1)"]}, headers={"X-ADT4-Goal-Origin": "operator"})
    ev = events(evroot, "console")
    by = {e["type"]: e["payload"] for e in ev}
    assert by["op.scene_repair"]["rid"] == "r9" and by["op.scene_repair"]["decision"] == "decline"
    assert by["op.take_lease"]["robot"] == "euclid" and by["op.take_lease"]["status"] >= 400  # no ROS here
    assert by["goal.approved"]["by"] == "operator" and by["goal.rejected"]["status"] >= 400


@needs_fleet_core
def test_launch_passes_session_id(settings, tmp_path, evroot):
    from .test_launch_topology import FakeSSH, _launch_deps

    ssh = FakeSSH([("echo OK", (0, "OK", "")), ("tmux has-session", (1, "", "")), ("test -d", (1, "", ""))])
    deps = _launch_deps(ssh)
    deps.deploy_zenoh = lambda user, ip, eps: (True, "deployed")
    deps.local_ips = lambda: set()
    settings.mission_log = True
    client, svc = make_client(settings, launch_deps=deps)
    with client:
        r = client.post("/api/launch", json={"robots": ["euclid"], "sessions": {"spot": "spot-default"},
                                             "experiment": "exp9", "mode": "mapping", "base_station": "willow",
                                             "bs_session": "base_station_web-default", "preflight": False})  # fmt: skip
        sid = r.json()["session_id"]
        job = svc.jobs.wait(r.json()["job_id"]).to_dict()
        assert job["state"] == "succeeded", [l["text"] for l in job["log"]]
    launched = ssh.ran("run-adt4")
    assert len(launched) == 2
    assert all(c.startswith(f"export ADT4_SESSION_ID={sid} && ") for c in launched)
    # the base station's run-adt4 -o is the logbook's base experiment dir
    base_cmd = [c for c in launched if "base_station_web-default" in c]
    assert len(base_cmd) == 1 and f"-o {settings.output_root}/exp9 " in base_cmd[0]
    assert svc.logbook.base_dir() == pathlib.Path(settings.output_root) / "exp9"
    assert svc.logbook.session["session_id"] == sid and svc.logbook.session["experiment"] == "exp9"
    s = json.loads((pathlib.Path(settings.output_root) / "exp9" / "sessions" / sid / "session.json").read_text())
    assert s["reloc"] == {"euclid": "not_required"} and "env" not in s
    time.sleep(0)
