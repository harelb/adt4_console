"""The chat's LLM rechecks a running goal against scene changes (scene_repair):
the operator's words go with every goal card, and recheck() reads the LLM's
answer as a new goal, or none."""

from __future__ import annotations

from adt4_console.chat import RECHECK_PROMPT, ChatTurn, operator_request
from tests.conftest import needs_heracles_agents
from tests.test_chat_online_team import chat, propose


def test_the_request_is_this_message_after_the_threads_earlier_ones():
    turn = ChatTurn(history=[{"role": "user", "text": "retrieve all the cones"},
                             {"role": "assistant", "text": "Send it?"}])  # fmt: skip
    assert (
        operator_request("yes, send it", turn)
        == "retrieve all the cones / yes, send it"
    )
    assert operator_request("go to o5") == "go to o5"


def test_scene_change_reports_are_not_taken_for_the_operators_words():
    turn = ChatTurn(history=[{"role": "user", "text": "[Scene change report] ..."}])
    assert operator_request("visit all chairs", turn) == "visit all chairs"


@needs_heracles_agents
def test_a_goal_card_carries_the_operators_words():
    c = chat()
    [card], _ = propose(
        c, "(visited-object o2)", message="the team visits all the chairs"
    )
    assert card["request"] == "the team visits all the chairs"
    assert "request" not in card["body"]


def _with_answer(events):
    c = chat()
    c.llm_client = lambda: object()
    c.stream = lambda message, **kw: iter(events)
    return c


def test_recheck_reads_a_proposed_goal():
    goal = "(and (visited-object o20) (visited-object o43))"
    c = _with_answer([{"type": "text", "text": "New cone O43: added."},
                      {"type": "goal", "kind": "pddl", "body": {"goal": goal, "constraints": []}},
                      {"type": "done"}])  # fmt: skip
    out = c.recheck("hamilton", {"pddl": "(visited-object o20)"}, "report")
    assert out == {"goal": goal, "constraints": [], "text": "New cone O43: added."}


def test_recheck_with_no_proposal_keeps_the_goal():
    c = _with_answer(
        [
            {"type": "text", "text": "O99 (chair) added: not related; no change."},
            {"type": "done"},
        ]
    )
    out = c.recheck("hamilton", {"pddl": "(visited-object o20)"}, "report")
    assert out["goal"] is None and "no change" in out["text"]


def test_recheck_without_an_llm_is_none():
    c = chat()
    c.llm_client = lambda: None
    assert c.recheck("hamilton", {"pddl": "(visited-object o20)"}, "r") is None
    assert not c.recheck_available()


def test_a_failed_llm_turn_is_none_so_the_rules_decide():
    c = _with_answer([{"type": "error", "text": "RateLimitError"}, {"type": "done"}])
    assert c.recheck("hamilton", {"pddl": "(visited-object o20)"}, "r") is None


def test_the_prompt_carries_the_report():
    assert "Robot: hamilton" in RECHECK_PROMPT.format(report="Robot: hamilton")
