"""at-place / at-object in the console chat (field transcript 2026-11-11).

"send euclid to be at object 20" became (visited-object-by euclid o20), and "I want
the at predicate" got "the PDDL interface ... does not support an at-object
predicate". Fast Downward does take at-place / at-object: the console's own goal
text (propose_goal's description and CONSOLE_GOAL_NOTES) stated GRSTAPS-X's limits
so that they read as limits of propose_goal. The rules for when to use at-* are
heracles_agents' (heracles_agents.prompts); here the console's text must not
contradict them, for either engine."""

from __future__ import annotations

import re
import types

from adt4_console.chat import CONSOLE_GOAL_NOTES, ChatService
from tests.conftest import needs_heracles_agents
from tests.test_chat_roster import chat

DESC = ChatService._T_PROPOSE_GOAL["description"]
_NEGATIVE = re.compile(r"\b(?:not take|takes no|does not support|not supported|no at-)", re.I)


def _sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.;])\s+|\n", text) if s.strip()]


def _no_unattributed_at_limit(text):
    """Every sentence that says at-* is not taken names GRSTAPS-X (or "this
    planner" in GRSTAPS-X's own domain text)."""
    for s in _sentences(text):
        if "at-" in s and _NEGATIVE.search(s):
            assert "GRSTAPS-X" in s or "This planner" in s, s


def _system(engine):
    c = chat()
    c._ctx.engine = engine  # the chat's planner toggle
    return c, c._system(None, "euclid")


def test_propose_goal_description_supports_at_on_fast_downward():
    assert "(at-place euclid t40) / (at-object euclid o20): that robot ends there (Fast Downward)" in DESC
    assert "With engine='grstaps' (GRSTAPS-X" in DESC
    assert "GRSTAPS-X takes no or / at-* / holding / use-robots (use engine='fast_downward' for those)" in DESC
    assert "it does not take or / at-*" not in DESC
    _no_unattributed_at_limit(DESC)


def test_console_goal_notes_name_fast_downward_for_at():
    assert "at-place / at-object (that robot ends there)" in CONSOLE_GOAL_NOTES
    assert "but GRSTAPS-X takes no or / at-* / holding / use-robots: those need Fast Downward" in CONSOLE_GOAL_NOTES
    assert "but takes no or / at-*" not in CONSOLE_GOAL_NOTES
    _no_unattributed_at_limit(CONSOLE_GOAL_NOTES)


@needs_heracles_agents
def test_fast_downward_prompt_presents_at_place_and_at_object_as_supported():
    _, text = _system("fast_downward")
    assert "The rules above are for fast_downward" in text
    # the console's notes and heracles_agents' domain + team goal rules
    assert "at-place / at-object (that robot ends there)" in text
    assert "(at-place ?r ?t): Robot ?r must END at place ?t" in text
    assert "(at-object ?r ?o): Robot ?r must END at object ?o" in text
    assert "use (at-place <robot> <t>) / (at-object <robot> <o>)" in text
    assert '"Euclid, be at O20": (at-object euclid o20)' in text
    assert '"Hamilton, wait at t7": (at-place hamilton t7)' in text
    assert "If the user explicitly asks for the at predicate, use it." in text
    # "go to" stays a visit
    assert '"Hamilton, go to O21. Gauss, inspect O26": (and (visited-object-by hamilton o21)' in text
    _no_unattributed_at_limit(text + "\n" + DESC)


@needs_heracles_agents
def test_grstaps_prompt_states_the_limit_and_names_fast_downward():
    _, text = _system("grstaps")
    assert "The rules above are for grstaps" in text
    assert "GRSTAPS-X cannot hold a robot at a final position" in text
    assert "Fast Downward can (the console's planner toggle)" in text
    assert "those need Fast Downward" in text
    assert "(at-object euclid o20)" not in text  # no at-* example for GRSTAPS-X
    _no_unattributed_at_limit(text + "\n" + DESC)


@needs_heracles_agents
def test_at_object_goal_on_fast_downward_makes_a_card_for_euclid():
    c, _ = _system("fast_downward")
    checked = []

    def goal_problems(goal, constraints_json, conf):
        checked.append(goal)
        return ""  # the map has euclid and o20

    c.goal_check = goal_problems
    c._cypher_src = lambda src: types.SimpleNamespace(id="map", uri="bolt://x", creds=("u", "p"))
    c._ctx.pddl_goals = []
    events, out = c._run_tool("propose_goal", {"kind": "pddl", "pddl_goal": "(at-object euclid o20)"},
                              "euclid, be at O20", "hamilton", "map", None)  # fmt: skip
    assert checked == ["(at-object euclid o20)"]
    [card] = events
    assert card["type"] == "goal" and card["kind"] == "pddl" and card["robot"] == "euclid"
    assert card["body"]["goal"] == "(at-object euclid o20)"
    assert card["body"]["engine"] == "fast_downward"
    assert "team" not in card  # one named robot
    assert out["proposed"] and out["sent_through"] == "euclid"
