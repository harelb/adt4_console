"""goal_tracker: one ``goal_event`` per robot whose part of a goal ended
(reached / failed / preempted, or planning failed), and arrival by position for
navigation-only plans of robots without per-action results (a Husky)."""

from __future__ import annotations

from types import SimpleNamespace

from adt4_console.goal_tracker import ARRIVE_M, GoalTracker, plan_end


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def step(self, dt=1.0):
        self.t += dt
        return self.t


def follow(pid, *pts):
    return {
        "plan_id": pid,
        "actions": [{"type": "FOLLOW", "path": [list(p) for p in pts]}],
    }


def result(pid, i, status="SUCCESS", typ="FOLLOW", detail=""):
    return {
        "plan_id": pid,
        "action_index": i,
        "action_type": typ,
        "status": status,
        "detail": detail,
    }


def make():
    clock = Clock()
    events = []
    tr = GoalTracker(publish=lambda gid, v: None, clock=clock, notify=events.append)
    return tr, clock, events


def test_plan_end_is_the_last_waypoint_or_point():
    assert plan_end(follow("p", (0, 0, 0), (3, 4, 0))) == (3.0, 4.0)
    pick = {
        "actions": [
            {"type": "FOLLOW", "path": [[1, 1, 0]]},
            {"type": "PICK", "path": [], "point": [5, 6, 0]},
        ]
    }
    assert plan_end(pick) == (5.0, 6.0)
    assert plan_end({"actions": []}) is None


def test_spot_goal_reached_by_action_results_is_announced_once():
    tr, clock, events = make()
    gid = tr.goal_sent("hamilton", kind="goto", summary="go to the tree")
    clock.step()
    tr.on_plan_status({"robot": "hamilton", "ok": True})
    tr.on_plan("hamilton", follow("p1", (0, 0, 0), (5, 0, 0)))
    clock.step()
    tr.on_result("hamilton", result("p1", 0))
    assert [(e["goal"], e["robot"], e["stage"]) for e in events] == [
        (gid, "hamilton", "done")
    ]
    assert events[0]["summary"] == "go to the tree"
    assert events[0]["t"] == clock.t
    # more updates of the same finished goal: no second event
    tr.on_result("hamilton", result("p1", 0))
    tr.on_delivered("hamilton")
    assert len(events) == 1
    assert tr.recent_notices() == events


def test_failed_action_is_announced_as_failed():
    tr, clock, events = make()
    tr.goal_sent("hamilton", kind="pddl", summary="pick the cone")
    tr.on_plan("hamilton", follow("p1", (0, 0, 0), (5, 0, 0)))
    tr.on_result("hamilton", result("p1", 0, status="FAILURE", detail="blocked"))
    assert [(e["stage"], e["text"]) for e in events] == [
        ("failed", "failed at action 1/1 (FOLLOW): blocked")
    ]


def test_planning_failure_is_announced():
    tr, clock, events = make()
    gid = tr.goal_sent("euclid", kind="pddl", summary="visit the shed")
    tr.on_plan_status({"robot": "euclid", "ok": False, "error": "no plan found"})
    assert [(e["goal"], e["robot"], e["stage"]) for e in events] == [
        (gid, "euclid", "failed")
    ]
    assert "no plan found" in events[0]["text"]


def test_husky_reached_by_position():
    tr, clock, events = make()
    gid = tr.goal_sent(
        "gauss", kind="goto", summary="go to the gate", platforms={"gauss": "phoenix"}
    )
    tr.on_plan("gauss", follow("h1", (0, 0, 0), (10, 0, 0)))
    assert tr.wants_pose() == ["gauss"]
    tr.on_pose("gauss", (5.0, 0.0))
    assert events == []
    tr.on_pose("gauss", None)  # not relocalized: nothing
    assert events == []
    clock.step()
    tr.on_pose("gauss", (10.0 - ARRIVE_M + 0.1, 0.2))
    assert [(e["goal"], e["robot"], e["stage"]) for e in events] == [
        (gid, "gauss", "done")
    ]
    assert events[0]["text"].startswith("reached the goal")
    assert tr.view(gid)["robots"]["gauss"]["stage"] == "done"
    assert tr.wants_pose() == []


def test_robot_with_action_results_is_not_reached_by_position():
    tr, clock, events = make()
    tr.goal_sent("hamilton", kind="goto")
    tr.on_plan("hamilton", follow("p1", (0, 0, 0), (5, 0, 0), (9, 0, 0)))
    tr.on_result("hamilton", result("p1", 0, status="RUNNING"))
    assert tr.wants_pose() == []
    tr.on_pose("hamilton", (9.0, 0.0))
    assert events == []


def test_non_navigation_plan_is_not_reached_by_position():
    tr, clock, events = make()
    tr.goal_sent("gauss", kind="pddl")
    tr.on_plan(
        "gauss",
        {
            "plan_id": "x",
            "actions": [
                {"type": "FOLLOW", "path": [[1, 0, 0]]},
                {"type": "PICK", "point": [1, 0, 0]},
            ],
        },
    )
    assert tr.wants_pose() == []


def test_check_goal_arrival_feeds_relocalized_positions(monkeypatch):
    from adt4_console import app

    tr, clock, events = make()
    tr.goal_sent("gauss", kind="goto", summary="gate")
    tr.on_plan("gauss", follow("h1", (0, 0, 0), (10, 0, 0)))
    monkeypatch.setattr(app, "relocalized_xy", lambda svc, robot: (10.0, 0.0))
    svc = SimpleNamespace(ros=SimpleNamespace(goals=tr))
    assert app.check_goal_arrival(svc) == ["gauss"]
    assert [e["stage"] for e in events] == ["done"]
    assert app.check_goal_arrival(SimpleNamespace(ros=None)) == []
