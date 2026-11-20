"""Fixes after the 2026-11-19 afternoon trial (mission3):

- hamilton's fleetlink link dropped for 9-20 s at a time; the chat called it
  offline and gave all its cones to euclid without asking;
- the LLM recheck of an added cone said "I will update the goal" but proposed
  none, and the card read "The goal stays the same";
- a repair goal sent from the chat carried only the last chat messages, so the
  mission's "visit all bags at building facade 2" was lost to later rechecks."""

from __future__ import annotations

from types import SimpleNamespace as NS

from adt4_console.chat import fleet_roster
from adt4_console.ros_bridge import RosBridge
from tests.conftest import needs_heracles_agents
from tests.test_chat_online_team import Live, chat, propose
from tests.test_chat_recheck import _with_answer
from tests.test_chat_roster import Ros, topology

STALE = {
    "euclid": {"connected": True},
    "hamilton": {"connected": False, "staleness_s": 14.0},
    "gauss": {"connected": False, "staleness_s": 900.0},
}


# ------------------------------------------------------------ link stale
def test_a_recently_dropped_link_is_stale_not_offline():
    rows = {r["robot"]: r for r in fleet_roster(Ros(topology()), Live(STALE))}
    assert rows["hamilton"]["online"] is False and rows["hamilton"]["stale_s"] == 14.0
    assert rows["gauss"]["stale_s"] is None  # gone 15 min: offline
    assert rows["euclid"]["stale_s"] is None
    text = chat(Live(STALE))._system(None, "euclid")
    ham = next(x for x in text.splitlines() if x.startswith("- hamilton:"))
    assert "LINK STALE (last heard 14 s ago)" in ham and "; offline" not in ham
    flat = " ".join(text.split())
    assert "Link stale (not offline): hamilton." in flat
    assert "never move its tasks to another robot on your own" in flat
    offline_line = next(x for x in text.splitlines() if x.startswith("The team now"))
    assert "hamilton" not in offline_line.split("do not assign:")[1]


@needs_heracles_agents
def test_a_goal_for_a_stale_robot_is_proposed_with_a_warning():
    c = chat(Live(STALE))
    [card], out = propose(c, "(visited-object-by hamilton o31)", "hamilton visit O31")
    assert out["proposed"] is True and card["robot"] == "hamilton"
    assert "hamilton: link stale" in card["warning"]
    # without the operator naming it, a gone robot is still refused
    _cards, out = propose(chat(Live(STALE)), "(visited-object-by gauss o31)", "visit O31")
    assert "NOT PROPOSED" in out["error"]


# ------------------------------------------------- recheck without a goal
def _answers(*turns):
    """A chat whose recheck turns answer in order."""
    c = _with_answer([])
    seen = []
    it = iter(turns)

    def stream(message, **kw):
        seen.append(message)
        return iter(next(it))

    c.stream = stream
    return c, seen


SAID = ("Cone O753 was added and is outside intersection 2, so it must also be brought "
        "into intersection 2. I will update the goal to include it.")  # fmt: skip
NEW = "(and (object-in-place o749 t6792) (object-in-place o753 t6792))"


def test_a_change_described_without_a_goal_is_asked_for_again():
    c, seen = _answers(
        [{"type": "text", "text": SAID}, {"type": "done"}],
        [{"type": "goal", "kind": "pddl", "body": {"goal": NEW}}, {"type": "text", "text": "Added O753."}],
    )
    out = c.recheck("hamilton", {"pddl": "(object-in-place o749 t6792)"}, "report")
    assert out["goal"] == NEW and not out.get("unsure")
    assert len(seen) == 2 and "you did not call the goal tool" in seen[1] and SAID in seen[1]


def test_no_goal_even_when_asked_again_is_unsure():
    c, _seen = _answers(
        [{"type": "text", "text": SAID}],
        [{"type": "text", "text": "I will add O753 to the goal."}],
    )
    out = c.recheck("hamilton", {"pddl": "g"}, "report")
    assert out["goal"] is None and out["unsure"] is True and out["text"] == SAID


def test_a_no_change_answer_is_not_asked_again():
    c, seen = _answers(
        [{"type": "text", "text": "Bag O752 is not in building facade 2, so the current goal is still correct and needs no change."}],
    )
    out = c.recheck("hamilton", {"pddl": "g"}, "report")
    assert out["goal"] is None and not out.get("unsure") and len(seen) == 1


def test_an_unsure_recheck_card_never_says_the_goal_stays():
    from tests.test_scene_repair import _added, _pending, make_llm

    sr, sent, _asked, run = make_llm(
        lambda r, g, rep: {"goal": None, "constraints": None, "text": SAID, "unsure": True}
    )
    sr.on_applied("sget/chat", 5, [_added("o43", "cone")])
    run()
    (rec,) = _pending(sr)
    assert "goal stays the same" not in rec["reason"]
    assert "may need to change but proposed no new goal" in rec["reason"]
    assert SAID in rec["reason"] and sent == []


# ------------------------------------------------ the mission's request kept
def _bridge(active):
    b = NS(_plan={r: NS(active_goal=g) for r, g in active.items()})
    return lambda goal, request: RosBridge._carried_request(b, goal, request)


MISSION = ("ask hamilton and euclid to bring all the cones outside of the intersection 2 into "
           "the intersection 2. ask gauss to visit all bags at building FACADE 2 and parking lot 5.")  # fmt: skip
MISSION_GOAL = "(and (object-placed-by hamilton o749 t6792) (visited-object-by gauss o128))"


def test_a_repair_goal_keeps_the_missions_request():
    carry = _bridge({"hamilton": {"pddl": MISSION_GOAL, "request": MISSION, "t": 1.0},
                     "euclid": None})  # fmt: skip
    repair = "(and (object-placed-by euclid o749 t6792) (object-placed-by euclid o753 t6792) (visited-object-by gauss o128))"
    got = carry(repair, "one cone / propose plan repairing based on context")
    assert got.startswith(MISSION) and got.endswith("// one cone / propose plan repairing based on context")
    assert len(got) <= 600


def test_a_new_request_sharing_no_object_keeps_only_its_words():
    carry = _bridge({"hamilton": {"pddl": MISSION_GOAL, "request": MISSION, "t": 1.0}})
    assert carry("(visited-object o31)", "go look at O31") == "go look at O31"
    assert carry("(at-place hamilton t2273)", "go to base camp") == "go to base camp"


def test_the_same_request_is_not_repeated():
    carry = _bridge({"hamilton": {"pddl": MISSION_GOAL, "request": MISSION, "t": 1.0}})
    assert carry(MISSION_GOAL, MISSION) == MISSION
    assert carry(MISSION_GOAL, None) == MISSION
