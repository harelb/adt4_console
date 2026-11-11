"""goal_tracker: a sent goal's live status from fake plan_status / plan /
action_result / teleop_state / runtime_guards messages."""

from __future__ import annotations

from adt4_console.goal_tracker import IDLE_S, GoalTracker


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def step(self, dt=1.0):
        self.t += dt
        return self.t


def plan(pid, *types):
    return {"plan_id": pid, "actions": [{"type": t} for t in types]}


def result(pid, i, status="SUCCESS", typ="FOLLOW", detail=""):
    return {"plan_id": pid, "action_index": i, "action_type": typ, "status": status, "detail": detail}


def teleop(executing=True, paused=False, **kw):
    return {"plan_executing": executing, "plan_paused": paused, **kw}


def make():
    clock = Clock()
    pushed = []
    tr = GoalTracker(publish=lambda gid, v: pushed.append((gid, v["stage"], v)), clock=clock)
    return tr, clock, pushed


def stages(pushed, gid):
    out = []
    for g, s, _ in pushed:
        if g == gid and (not out or out[-1] != s):
            out.append(s)
    return out


def robot_stages(pushed, gid, robot):
    out = []
    for g, _, v in pushed:
        st = (v["robots"].get(robot) or {}).get("stage")
        if g == gid and st and (not out or out[-1] != st):
            out.append(st)
    return out


def test_single_robot_happy_path():
    tr, clock, pushed = make()
    gid = tr.goal_sent("euclid", kind="pddl", via="goal_manager", summary="(visited-place p1)")
    clock.step()
    tr.on_plan("euclid", plan("0026942e", "FOLLOW", "GAZE"))
    tr.on_plan_status({"plugin": "Pddl", "robot": "euclid", "ok": True})
    v = tr.view(gid)
    assert v["planner"] == "planned"
    assert v["robots"]["euclid"]["plan_id"] == "0026942e" and v["robots"]["euclid"]["n"] == 2
    clock.step()
    tr.on_teleop("euclid", teleop(executing=True))
    assert tr.view(gid)["robots"]["euclid"]["stage"] == "executing"
    assert "action 1/2 (FOLLOW)" in tr.view(gid)["robots"]["euclid"]["text"]
    clock.step()
    tr.on_result("euclid", result("0026942e", 0))
    assert "action 2/2 (GAZE)" in tr.view(gid)["robots"]["euclid"]["text"]
    clock.step()
    tr.on_result("euclid", result("0026942e", 1, typ="GAZE"))
    assert tr.view(gid)["stage"] == "done"
    assert stages(pushed, gid) == ["planning", "planned", "executing", "done"]


def test_paused_and_resumed_the_motivating_incident():
    tr, clock, pushed = make()
    gid = tr.goal_sent("euclid", kind="pddl")
    tr.on_plan("euclid", plan("0026942e", "FOLLOW", "FOLLOW"))
    tr.on_plan_status({"robot": "euclid", "ok": True})
    clock.step()
    tr.on_teleop("euclid", teleop(executing=True))
    clock.step(18)
    tr.on_teleop("euclid", teleop(executing=False, paused=True))
    v = tr.view(gid)
    assert v["stage"] == "paused" and v["robots"]["euclid"]["text"].startswith("paused at action 1/2")
    # still paused much later: never mistaken for a stopped executor
    clock.step(60)
    tr.on_teleop("euclid", teleop(executing=False, paused=True))
    assert tr.view(gid)["stage"] == "paused"
    clock.step()
    tr.on_teleop("euclid", teleop(executing=True))
    assert tr.view(gid)["stage"] == "executing"
    texts = [e["text"] for e in tr.view(gid)["robots"]["euclid"]["events"]]
    assert "paused" in texts and "resumed" in texts


def test_planning_failed():
    tr, clock, pushed = make()
    gid = tr.goal_sent("hamilton", kind="pddl")
    clock.step(3)
    tr.on_plan_status({"robot": "hamilton", "ok": False, "error": "no connectivity left in the problem"})
    v = tr.view(gid)
    assert v["stage"] == "plan_failed"
    assert "no connectivity" in v["text"]
    assert v["robots"]["hamilton"]["stage"] == "no_plan"


def test_status_for_another_robot_does_not_match():
    tr, clock, _ = make()
    gid = tr.goal_sent("hamilton", kind="pddl")
    tr.on_plan_status({"robot": "euclid", "ok": False, "error": "x"})
    assert tr.view(gid)["planner"] == "sent"


def test_action_failed():
    tr, clock, _ = make()
    gid = tr.goal_sent("euclid", kind="pddl")
    tr.on_plan("euclid", plan("p1", "FOLLOW", "PICK"))
    tr.on_plan_status({"robot": "euclid", "ok": True})
    tr.on_result("euclid", result("p1", 0))
    tr.on_result("euclid", result("p1", 1, "FAILED", "PICK", "gave up after 3 attempts"))
    v = tr.view(gid)
    assert v["stage"] == "failed"
    assert v["robots"]["euclid"]["text"] == "failed at action 2/2 (PICK): gave up after 3 attempts"


def test_preempted_by_a_newer_goal():
    tr, clock, pushed = make()
    g1 = tr.goal_sent("euclid", kind="pddl")
    tr.on_plan("euclid", plan("p1", "FOLLOW"))
    tr.on_plan_status({"robot": "euclid", "ok": True})
    tr.on_teleop("euclid", teleop())
    clock.step(10)
    g2 = tr.goal_sent("euclid", kind="pddl")
    clock.step()
    tr.on_plan("euclid", plan("p2", "FOLLOW", "FOLLOW"))
    tr.on_plan_status({"robot": "euclid", "ok": True})
    assert tr.view(g1)["robots"]["euclid"]["stage"] == "preempted"
    assert "newer goal" in tr.view(g1)["robots"]["euclid"]["text"]
    assert tr.view(g2)["robots"]["euclid"]["plan_id"] == "p2"
    # the executor's PREEMPTED result for p1 does not touch g2
    tr.on_result("euclid", result("p1", 0, "PREEMPTED"))
    assert tr.view(g2)["robots"]["euclid"]["stage"] in ("planned", "executing")


def test_preempted_result():
    tr, clock, _ = make()
    gid = tr.goal_sent("euclid", kind="pddl")
    tr.on_plan("euclid", plan("p1", "FOLLOW", "FOLLOW"))
    tr.on_plan_status({"robot": "euclid", "ok": True})
    tr.on_result("euclid", result("p1", 0, "PREEMPTED", detail="sequence pre-empted"))
    assert tr.view(gid)["stage"] == "preempted"


def test_replan_by_plan_repair_is_followed():
    tr, clock, _ = make()
    gid = tr.goal_sent("euclid", kind="pddl")
    tr.on_plan("euclid", plan("p1", "FOLLOW"))
    tr.on_plan_status({"robot": "euclid", "ok": True})
    clock.step(30)
    tr.on_plan("euclid", plan("p9", "FOLLOW", "GAZE"))  # no console goal: a replan
    rv = tr.view(gid)["robots"]["euclid"]
    assert rv["plan_id"] == "p9" and rv["n"] == 2
    assert any("replaced" in e["text"] for e in rv["events"])


def test_multi_robot_goal():
    tr, clock, pushed = make()
    gid = tr.goal_sent("euclid", robots=["euclid", "hamilton"], kind="pddl")
    clock.step()
    tr.on_plan("euclid", plan("pe", "FOLLOW"))
    tr.on_plan("hamilton", plan("ph", "FOLLOW", "GAZE"))
    tr.on_plan("gauss", plan("pg", "FOLLOW"))  # planned along with the goal
    tr.on_plan_status({"robot": "euclid", "ok": True})
    v = tr.view(gid)
    assert set(v["robots"]) == {"euclid", "hamilton", "gauss"}
    assert v["robots"]["gauss"]["named"] is False
    tr.on_teleop("euclid", teleop())
    tr.on_result("euclid", result("pe", 0))
    tr.on_teleop("hamilton", teleop(executing=False, paused=True))
    v = tr.view(gid)
    assert v["robots"]["euclid"]["stage"] == "done"
    assert v["robots"]["hamilton"]["stage"] == "paused"
    assert v["stage"] == "paused"  # the overall line shows what needs a look
    tr.on_teleop("hamilton", teleop())
    tr.on_result("hamilton", result("ph", 0))
    tr.on_result("hamilton", result("ph", 1, typ="GAZE"))
    tr.on_result("gauss", result("pg", 0))
    assert tr.view(gid)["stage"] == "done"


def test_tablet_takes_the_lease_and_gives_it_back():
    tr, clock, pushed = make()
    gid = tr.goal_sent("euclid", kind="pddl")
    tr.on_plan("euclid", plan("p1", "FOLLOW", "FOLLOW"))
    tr.on_plan_status({"robot": "euclid", "ok": True})
    tr.on_lease("euclid", {"known": True, "owned": True, "owner": "understanding-euclid"})
    tr.on_teleop("euclid", teleop())
    assert tr.view(gid)["stage"] == "executing"
    clock.step()
    tr.on_lease("euclid", {"known": True, "owned": False, "owner": "bosdyn.android.spotapp tablet"})
    rv = tr.view(gid)["robots"]["euclid"]
    assert rv["stage"] == "lease_wait"
    assert rv["text"] == "waiting — the tablet (bosdyn.android.spotapp tablet) holds the lease (tablet took over)"
    # the executor still runs the sequence (waiting between actions): not "stopped"
    clock.step(30)
    tr.on_teleop("euclid", teleop())
    assert tr.view(gid)["robots"]["euclid"]["stage"] == "lease_wait"
    clock.step()
    tr.on_lease("euclid", {"known": True, "owned": True, "owner": "understanding-euclid"})
    assert tr.view(gid)["robots"]["euclid"]["stage"] == "executing"
    assert robot_stages(pushed, gid, "euclid")[-3:] == ["executing", "lease_wait", "executing"]


def test_lease_unknown():
    tr, clock, _ = make()
    gid = tr.goal_sent("euclid", kind="pddl")
    tr.on_plan("euclid", plan("p1", "FOLLOW"))
    tr.on_plan_status({"robot": "euclid", "ok": True})
    tr.on_teleop("euclid", teleop())
    tr.on_lease("euclid", {"known": False, "owned": False, "owner": ""})
    rv = tr.view(gid)["robots"]["euclid"]
    assert rv["stage"] == "lease_unknown" and "lease state unknown" in rv["text"]


def test_lease_known_before_the_plan_applies():
    tr, clock, _ = make()
    tr.on_lease("euclid", {"known": True, "owned": False, "owner": "bosdyn.android.spotapp x"})
    gid = tr.goal_sent("euclid", kind="pddl")
    tr.on_plan("euclid", plan("p1", "FOLLOW"))
    tr.on_teleop("euclid", teleop())
    assert tr.view(gid)["robots"]["euclid"]["stage"] == "lease_wait"


def test_executor_stopped_without_result():
    tr, clock, _ = make()
    gid = tr.goal_sent("euclid", kind="pddl")
    tr.on_plan("euclid", plan("p1", "FOLLOW", "FOLLOW"))
    tr.on_plan_status({"robot": "euclid", "ok": True})
    tr.on_teleop("euclid", teleop())
    clock.step(IDLE_S + 1)
    tr.on_teleop("euclid", teleop(executing=False))
    assert tr.view(gid)["robots"]["euclid"]["stage"] == "stopped"


def test_fleetlink_needs_delivery_before_teleop_counts():
    tr, clock, _ = make()
    gid = tr.goal_sent("euclid", kind="pddl", platforms={"fleetlink_euclid": True})
    tr.on_plan("euclid", plan("p2", "FOLLOW"))
    tr.on_plan_status({"robot": "euclid", "ok": True})
    clock.step()
    # the executor is still on the previous plan: no proof it has this one
    tr.on_teleop("euclid", teleop())
    assert tr.view(gid)["robots"]["euclid"]["stage"] == "planned"
    tr.on_delivered("euclid")
    assert tr.view(gid)["robots"]["euclid"]["stage"] == "delivered"
    clock.step()
    tr.on_teleop("euclid", teleop())
    assert tr.view(gid)["robots"]["euclid"]["stage"] == "executing"
    assert tr.view(gid)["robots"]["euclid"]["received"] is not None


def test_exact_receipt_from_teleop_last_event():
    tr, clock, _ = make()
    gid = tr.goal_sent("euclid", kind="pddl", platforms={"fleetlink_euclid": True})
    tr.on_plan("euclid", plan("p2", "FOLLOW"))
    clock.step()
    tr.on_teleop("euclid", teleop(last_event="new plan p2 received"))
    rv = tr.view(gid)["robots"]["euclid"]
    assert rv["received"] is not None and rv["received_how"] == "executor reports the new plan"


def test_skipped_goal_and_fifo_status():
    tr, clock, _ = make()
    g1 = tr.goal_sent("euclid", kind="pddl")
    clock.step()
    g2 = tr.goal_sent("euclid", kind="pddl")
    clock.step()
    tr.on_plan("euclid", plan("p2", "FOLLOW"))  # the newest goal's plan
    tr.on_plan_status({"robot": "euclid", "ok": True})
    assert tr.view(g2)["planner"] == "planned"
    assert tr.view(g1)["planner"] == "skipped"
    assert tr.view(g1)["stage"] == "plan_failed"


def test_console_stop():
    tr, clock, _ = make()
    gid = tr.goal_sent("euclid", kind="pddl")
    tr.on_plan("euclid", plan("p1", "FOLLOW"))
    tr.on_executor_cmd("euclid", "pause")
    tr.on_executor_cmd("euclid", "stop")
    rv = tr.view(gid)["robots"]["euclid"]
    assert rv["stage"] == "preempted" and rv["text"] == "stopped from the console"
    assert any(e["text"] == "pause sent from the console" for e in rv["events"])


def test_direct_route_has_no_planner():
    tr, clock, _ = make()
    gid = tr.goal_sent("euclid", kind="direct_path", direct=True)
    tr.on_plan("euclid", plan("d1", "FOLLOW"))
    tr.on_plan_status({"robot": "euclid", "ok": False, "error": "unrelated"})
    v = tr.view(gid)
    assert v["planner"] == "planned" and v["robots"]["euclid"]["plan_id"] == "d1"


def test_send_failed_and_no_duplicate_pushes():
    tr, clock, pushed = make()
    gid = tr.goal_sent("euclid", kind="pddl")
    tr.send_failed(gid, "nothing subscribes")
    assert tr.view(gid)["stage"] == "plan_failed"
    n = len(pushed)
    tr.on_teleop("euclid", teleop())
    tr.on_teleop("euclid", teleop())
    assert len(pushed) == n
