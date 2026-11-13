"""A chat turn's own state (planner toggle, mission context, reasoning effort, the
one-goal-per-reply guard) belongs to that turn, whatever thread runs it.

POST /api/chat streams ``ChatService.stream`` (a sync generator) through
Starlette's StreamingResponse, which calls ``next()`` on it from the threadpool:
each step of one turn may run on a different worker thread, and one worker
runs steps of different turns. The per-turn state used to live in a
``threading.local`` set at the turn's first step, so a tool call after the first
streamed event lost it (another thread) or saw another turn's (same thread).
These tests drive ``stream`` the way the threadpool does."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace as NS

import pytest

from adt4_console.chat import ChatService
from tests.conftest import needs_heracles_agents
from tests.test_chat_roster import Ros, topology

pytest.importorskip("anthropic")


class Messages:
    """client.beta.messages: scripted replies, in order."""

    def __init__(self, script):
        self.script = list(script)

    def create(self, **kw):
        content, stop = self.script.pop(0)
        return NS(content=content, stop_reason=stop, model="claude-test",
                  usage=NS(input_tokens=10, output_tokens=10))  # fmt: skip


def _propose(i, goal):
    return NS(type="tool_use", id=f"g{i}", name="propose_goal",
              input={"kind": "pddl", "pddl_goal": goal})  # fmt: skip


def _script(*goals):
    """One reply: a line of text, then a propose_goal per goal; then "Done."."""
    first = [NS(type="text", text="Proposing.")] + [
        _propose(i, g) for i, g in enumerate(goals)
    ]
    return [(first, "tool_use"), ([NS(type="text", text="Done.")], "end_turn")]


def _chat(tmp_path, script):
    settings = NS(llm=True, llm_provider="anthropic", llm_model=None,
                  llm_usage_path=str(tmp_path / "usage.json"), llm_budget_usd=10.0)  # fmt: skip
    c = ChatService(settings, NS(list=lambda: []), None, Ros(topology()))  # no map
    c.provider = lambda: "anthropic"
    c._client, c._client_provider = NS(beta=NS(messages=Messages(script))), "anthropic"
    return c


def _each_step_on_a_new_thread(gen):
    """What iterate_in_threadpool may do: every next() on another worker thread."""
    out = []
    while True:
        box = {}

        def step():
            try:
                box["item"] = next(gen)
            except StopIteration:
                box["done"] = True

        t = threading.Thread(target=step)
        t.start()
        t.join()
        if box.get("done"):
            return out
        out.append(box["item"])


def _cards(events):
    return [e for e in events if e["type"] == "goal"]


@needs_heracles_agents
def test_a_tool_call_on_another_thread_keeps_the_turns_engine_and_guard(tmp_path):
    """The goal is proposed after the turn's first streamed event, on another
    thread: it still gets the turn's engine, and the second per-robot goal of the
    same reply is still refused (one team goal per reply)."""
    c = _chat(
        tmp_path,
        _script("(visited-object-by euclid o2)", "(visited-object-by hamilton o3)"),
    )
    events = _each_step_on_a_new_thread(
        c.stream("euclid to o2, hamilton to o3", robot="euclid", engine="grstaps")
    )
    cards = _cards(events)
    assert len(cards) == 1, cards  # the second goal is NOT PROPOSED
    assert cards[0]["body"] == {
        "goal": "(visited-object-by euclid o2)",
        "engine": "grstaps",
    }
    transcript = next(e for e in events if e["type"] == "transcript")["items"]
    refused = [i for i in transcript if i.get("role") == "tool" and i["id"] == "g1"]
    assert "NOT PROPOSED: this reply already proposed the goal" in refused[0]["content"]


@needs_heracles_agents
def test_two_turns_on_one_worker_thread_keep_their_own_engines(tmp_path):
    """Two turns of the app's one ChatService interleave on one threadpool worker:
    each goal gets its own turn's planner toggle (not the turn that started last),
    and the second turn's goal is not taken for a second goal of the first."""
    (a1, a2), (b1, b2) = _script("(visited-object o2)"), _script("(visited-object o3)")
    c = _chat(tmp_path, [a1, b1, a2, b2])  # the order the steps below ask for them
    a = c.stream("visit o2", robot="euclid", engine="grstaps")
    b = c.stream("visit o3", robot="euclid", engine="fast_downward")
    out = {id(a): [], id(b): []}
    with ThreadPoolExecutor(max_workers=1) as worker:
        for gen in (a, b):  # each turn's first step: up to its first text
            out[id(gen)].append(worker.submit(next, gen).result())
        for gen in (a, b):  # then the rest of a, then the rest of b
            while True:
                try:
                    out[id(gen)].append(worker.submit(next, gen).result())
                except StopIteration:
                    break
    bodies = {k: [card["body"] for card in _cards(v)] for k, v in out.items()}
    assert bodies[id(a)] == [{"goal": "(visited-object o2)", "engine": "grstaps"}]
    # b's goal is not refused as "a second goal in this reply" of a's turn
    assert bodies[id(b)] == [{"goal": "(visited-object o3)", "engine": "fast_downward"}]
