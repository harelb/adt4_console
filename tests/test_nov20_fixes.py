"""Nov20 sim findings: a card never offers a goal naming a removed object; a
grasp request is withdrawn when the robot's action ends without it."""

from __future__ import annotations

import pytest

from adt4_console.scene_repair import NotPending, drop_removed
from tests.test_manipulation import make_service, request
from tests.test_scene_repair import REMOVED, make

TEAM = "(and (object-placed-by hamilton o20 p1350) (visited-object-by euclid o5))"


def test_drop_removed_keeps_the_rest():
    assert drop_removed(TEAM, {"O20"}) == "(visited-object-by euclid o5)"
    assert drop_removed("(and (visited-object o59) (visited-object o5))", {"O5"}) == "(visited-object o59)"
    assert drop_removed("(visited-object o20)", {"o20"}) is None
    assert drop_removed(TEAM, set()) == TEAM
    assert drop_removed(TEAM, {"o2"}) == TEAM  # o2 is not o20


def test_confirming_a_card_never_sends_the_removed_object():
    goals = {"hamilton": {"pddl": TEAM, "constraints": [], "engine": None, "route": "auto"}}
    sr, _hub, sent, _ = make(goals=goals)
    (rec,) = sr.on_applied("console/operator", 5, [{"kind": REMOVED, "symbol": "O20", "label": "cone"}],
                           summary="remove O20 (cone)")  # fmt: skip
    sr.confirm(rec["id"])
    assert sent[-1][1] == "(visited-object-by euclid o5)"


def test_confirming_when_nothing_is_left_sends_nothing():
    sr, _hub, sent, _ = make()
    (rec,) = sr.on_applied("awcd/hamilton", 5, [{"kind": REMOVED, "symbol": "O20", "label": "cone"}])
    with pytest.raises(NotPending, match="nothing is left"):
        sr.confirm(rec["id"])
    assert sent == [] and _status(sr, rec["id"]) == "pending"


def _status(sr, rid):
    return next(r["status"] for r in sr.state()["recommendations"] if r["id"] == rid)


def test_a_preempted_robots_grasp_request_is_withdrawn():
    svc = make_service()
    cur = svc.receive_request("hamilton", request())
    other = svc.receive_request("euclid", request())
    assert svc.withdraw("hamilton", "pick preempted") == 1
    ids = [r["id"] for r in svc.pending()["requests"]]
    assert cur["id"] not in ids and other["id"] in ids
    assert svc.withdraw("hamilton", "again") == 0


def test_a_recheck_may_leave_only_another_robots_part_of_the_team_goal():
    from tests.test_chat_recheck import _with_answer

    left = [{"type": "goal", "kind": "pddl", "robot": "euclid",
             "body": {"goal": "(visited-object-by euclid o5)"}},
            {"type": "text", "text": "O20 is gone: only euclid's visit is left."}]  # fmt: skip
    out = _with_answer(left).recheck("hamilton", {"pddl": TEAM}, "r")
    assert out["goal"] == "(visited-object-by euclid o5)"
    # a robot the goal never named is still not used
    assert _with_answer(left).recheck("hamilton", {"pddl": "(visited-object o20)"}, "r") is None


# ---------------------------------------------- reviews of the Nov 20 console
from types import SimpleNamespace as NS

from adt4_console.chat import keeps_goal
from adt4_console.ros_bridge import RosBridge

MISSION = "bring all the cones outside of the intersection 2 into the intersection 2"
MISSION_GOAL = "(and (object-in-place o749 t2433) (at-place hamilton t2273) (at-place euclid t2273))"


def _carry(active, busy):
    b = NS(_plan={r: NS(active_goal=g) for r, g in active.items()},
           goals=NS(busy_robots=lambda: dict.fromkeys(busy, "g")))  # fmt: skip
    return lambda goal, request: RosBridge._carried_request(b, goal, request)


def test_keeps_goal_only_for_an_answer_ending_with_no_change():
    assert keeps_goal("O99 (chair) added: not related to the request; no change.")
    assert keeps_goal("no change")
    assert keeps_goal("Bag O752 is outside the regions asked for, so the goal needs no change.")
    # phrases elsewhere no longer stop the second ask (both read "goal stays" on Nov 20)
    assert not keeps_goal("O752 (bag) is not related; cone O753 is outside intersection 2, I will update the goal")
    assert not keeps_goal("Keeping the robot assignment unchanged, O753 must be added to the goal.")
    assert not keeps_goal("")


def test_a_new_request_containing_the_old_one_keeps_the_new_words():
    carry = _carry({"hamilton": {"pddl": MISSION_GOAL, "request": "visit all bags", "t": 1.0}}, ["hamilton"])
    got = carry("(and (object-in-place o749 t2433) (visited-object o3))", "visit all bags / also visit all cones")
    assert got == "visit all bags / also visit all cones"


def test_a_finished_missions_request_is_not_carried():
    active = {"hamilton": {"pddl": MISSION_GOAL, "request": MISSION, "t": 1.0}}
    assert _carry(active, [])("(object-placed-by hamilton o749 t1)", "bring O749 back to base") == \
        "bring O749 back to base"
    assert _carry(active, ["hamilton"])("(object-placed-by hamilton o749 t1)", "bring O749 back to base").startswith(MISSION)


def test_another_robots_goal_on_other_objects_is_a_new_request():
    active = {"hamilton": {"pddl": MISSION_GOAL, "request": MISSION, "t": 1.0}, "euclid": None}
    assert _carry(active, ["hamilton"])("(object-placed-by gauss o753 t1)", "gauss move O753") == "gauss move O753"
    assert _carry(active, ["hamilton"])("(at-place hamilton t2273)", "go to base camp") == "go to base camp"


def test_an_unsure_card_keeps_the_rules_findings_and_confirm_sends_nothing():
    from tests.test_scene_repair import make_llm

    sr, sent, _asked, run = make_llm(
        lambda r, g, rep: {"goal": None, "constraints": None, "text": "O20 is gone; I will replace it", "unsure": True}
    )
    sr.on_applied("awcd/hamilton", 5, [{"kind": REMOVED, "symbol": "O20", "label": "cone"}])
    run()
    (rec,) = [r for r in sr.state()["recommendations"] if r["status"] == "pending"]
    assert rec.get("unsure") and any("O20" in x for x in rec["reasons"][:-1])
    with pytest.raises(NotPending, match="ask the chat"):
        sr.confirm(rec["id"])
    assert sent == [] and _status(sr, rec["id"]) == "pending"


def test_an_empty_question_message_keeps_answered_ids():
    import json as _j

    sr, *_ = make()
    sr._held_done["q1"] = "confirm"
    sr.on_replan_pending("")
    sr.on_replan_pending(_j.dumps({"id": "q1", "robots": ["hamilton"], "reason": "r"}))
    assert sr._hold() is None  # a planner restart republishing q1 is not asked again


def test_a_stale_robot_is_offered_only_for_its_own_work():
    from tests.test_chat_online_team import chat

    c = chat()
    c._busy = lambda: {"hamilton": "g"}
    assert c._available({"robot": "hamilton", "online": False, "stale_s": 20.0})
    assert not c._available({"robot": "euclid", "online": False, "stale_s": 20.0})


# ------------------------------------------------ v3 (review of 3d0e286)
def test_a_grasp_request_of_a_newer_plan_is_not_withdrawn():
    # the old plan's PREEMPTED arrives after the new plan's Pick asked (fleetlink
    # relays the two topics separately): the new request stays
    svc = make_service()
    new = svc.receive_request("hamilton", request(), plan_id="p2")
    assert svc.withdraw("hamilton", "pick preempted", plan_id="p1") == 0
    assert new["id"] in [r["id"] for r in svc.pending()["requests"]]
    assert svc.withdraw("hamilton", "pick failed", plan_id="p2") == 1


def test_only_a_pick_result_withdraws_a_grasp_request():
    from types import SimpleNamespace as NS
    from adt4_console.ros_bridge import RosBridge, RobotPlanState

    calls = []
    b = RosBridge.__new__(RosBridge)
    b._plan = {"hamilton": RobotPlanState()}
    b.__dict__["_goals"] = NS(on_result=lambda *a, **k: None)
    b.manipulation = NS(withdraw=lambda robot, why, plan_id=None: calls.append((robot, why, plan_id)))
    b._publish_plan = lambda robot: None
    hdr = NS(stamp=NS(sec=1, nanosec=0))
    for i, (typ, st) in enumerate([("GAZE", "FAILED"), ("FOLLOW", "PREEMPTED"), ("PICK", "SUCCESS"), ("PICK", "PREEMPTED")]):
        b._on_result("hamilton", NS(plan_id="p1", action_index=i, action_type=typ, status=st, detail="", header=hdr))
    assert calls == [("hamilton", "pick preempted", "p1")]


def test_an_uppercase_and_goal_is_split():
    from adt4_console.scene_repair import _conjuncts, drop_removed

    assert _conjuncts("(AND (visited-object o1) (visited-object o2))") == ["(visited-object o1)", "(visited-object o2)"]
    assert drop_removed("(AND (visited-object o1) (visited-object o2))", {"o1"}) == "(visited-object o2)"


def test_benign_no_change_answers_keep_the_goal():
    for t in ("**No change.**", "No change to the goal.", "No change: O99 is unrelated.",
              "O99 (chair) added; no change needed.",
              "O99 (cone) added outside intersection 2, so it should not be added; no change.",
              "O99 is a chair, so it does not need to be included; no change."):  # fmt: skip
        assert keeps_goal(t), t
    assert not keeps_goal("O753 must be added; otherwise no change")


def test_a_contraction_is_a_negation_too():
    assert keeps_goal("O99 is not related, so it doesn't need to be added; no change.")


def test_a_team_goal_through_an_idle_robot_carries_only_while_its_robots_work():
    from tests.test_nov19_fixes import MISSION, MISSION_GOAL, _bridge

    team = {"pddl": MISSION_GOAL, "request": MISSION, "t": 2.0,
            "constraints": [{"predicate": "use-robots", "symbols": ["hamilton", "euclid"]}]}  # fmt: skip
    # sent through gauss, which got no work; euclid (in use-robots) works on it
    carry = _bridge({"gauss": team, "euclid": None}, busy=["euclid"])
    assert carry(MISSION_GOAL, "use hamilton and euclid equally").startswith(MISSION)
    # only an unrelated robot works: the finished team goal's words are not carried
    carry = _bridge({"gauss": team, "apollo": None}, busy=["apollo"])
    assert carry(MISSION_GOAL, "bring O749 back") == "bring O749 back"
