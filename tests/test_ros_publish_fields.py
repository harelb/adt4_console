"""A goal with constraints must not break the console (robot test 2026-11-11).

POST /api/robots/euclid/goal/pddl with constraints [{predicate: use-robots,
symbols: [euclid]}] returned 500 "can not serialize ConstraintFact object", and
after that every send failed with "ROS executor stopped". RosBridge.publish()
gave the caller's fields to set_message_fields, which (rosidl_runtime_py 0.13)
replaces the constraint dicts with ConstraintFact messages in place; _send keeps
those fields for the hub (msgpack), so the HTTP call failed, then _on_plan's
_publish_plan raised in the executor and stopped it."""

from __future__ import annotations

import json
import sys
import threading
import time
import types

import msgpack
import pytest

from adt4_console import ros_bridge
from adt4_console.ros_bridge import RosBridge

FIELDS = {
    "goal": {"robot_id": "euclid", "pddl_goal": "(visited-object o2)"},
    "constraints": [
        {"predicate": "use-robots", "symbols": ["euclid"]},
        {"predicate": "before", "symbols": ["o2", "o8"]},
        {"predicate": "forbidden-poi", "symbols": ["t12"]},
    ],
}


def _real_rosidl():
    try:
        from omniplanner_msgs.msg import ConstrainedPddlGoalMsg  # noqa: F401
        from rosidl_runtime_py.set_message import set_message_fields  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


class _Fact:
    """Stands in for omniplanner_msgs/ConstraintFact (not msgpack-able)."""

    def __init__(self):
        self.predicate, self.symbols = "", []


class _Goal:
    def __init__(self):
        self.robot_id, self.pddl_goal = "", ""


class _GoalMsg:
    def __init__(self):
        self.goal, self.constraints = _Goal(), []


def _fake_set_message_fields(msg, values):
    """rosidl_runtime_py 0.13.1's set_message_fields on a sequence of messages:
    each dict of ``values[field]`` is replaced in place by the message built
    from it (``field_value[n] = submsg``), then the list is assigned."""
    for name, value in values.items():
        if isinstance(value, list):
            for n in range(len(value)):
                sub = _Fact()
                for k, v in value[n].items():
                    setattr(sub, k, v)
                value[n] = sub
            setattr(msg, name, value)
        elif isinstance(value, dict):
            sub = getattr(msg, name)
            for k, v in value.items():
                setattr(sub, k, v)
        else:
            setattr(msg, name, value)


@pytest.fixture(params=["rosidl", "faithful_fake"])
def bridge(request, monkeypatch):
    if request.param == "rosidl":
        if not _real_rosidl():
            pytest.skip(
                "rosidl_runtime_py / omniplanner_msgs not importable (source ROS + dcist_ws)"
            )
        from omniplanner_msgs.msg import ConstrainedPddlGoalMsg

        cls = ConstrainedPddlGoalMsg
    else:
        mod = types.ModuleType("rosidl_runtime_py.set_message")
        mod.set_message_fields = _fake_set_message_fields
        pkg = types.ModuleType("rosidl_runtime_py")
        pkg.set_message = mod
        monkeypatch.setitem(sys.modules, "rosidl_runtime_py", pkg)
        monkeypatch.setitem(sys.modules, "rosidl_runtime_py.set_message", mod)
        cls = _GoalMsg
    published = []
    pub = types.SimpleNamespace(
        publish=published.append, get_subscription_count=lambda: 1
    )
    rb = RosBridge(types.SimpleNamespace(ros=False, planner_ns="willow"))
    monkeypatch.setattr(ros_bridge, "import_type", lambda path: cls)
    monkeypatch.setattr(rb, "run", lambda fn, timeout=5.0: fn())
    monkeypatch.setattr(rb, "_publisher", lambda topic, c: pub)
    return rb, published, request.param


def _check_fields_untouched(fields):
    assert fields == FIELDS
    assert all(type(c) is dict for c in fields["constraints"])
    json.dumps(fields)
    msgpack.packb(fields, use_bin_type=True)


def test_set_message_fields_mutates_the_callers_dicts(bridge):
    """The bug itself: without a copy, the caller's constraints become messages."""
    _, _, kind = bridge
    from rosidl_runtime_py.set_message import set_message_fields

    fields = json.loads(json.dumps(FIELDS))
    msg = ros_bridge.import_type("omniplanner_msgs/msg/ConstrainedPddlGoalMsg")()
    set_message_fields(msg, fields)
    assert type(fields["constraints"][0]) is not dict, kind
    with pytest.raises(TypeError):
        msgpack.packb(fields, use_bin_type=True)


def test_publish_leaves_constraint_fields_unchanged_and_serialisable(bridge):
    rb, published, _ = bridge
    fields = json.loads(json.dumps(FIELDS))
    assert (
        rb.publish(
            "/willow/goal", "omniplanner_msgs/msg/ConstrainedPddlGoalMsg", fields
        )
        == 1
    )
    [msg] = published
    assert [c.predicate for c in msg.constraints] == [
        "use-robots",
        "before",
        "forbidden-poi",
    ]
    assert list(msg.constraints[0].symbols) == ["euclid"]
    _check_fields_untouched(fields)


def test_publish_now_leaves_constraint_fields_unchanged(bridge):
    rb, published, _ = bridge
    fields = json.loads(json.dumps(FIELDS))
    rb.publish_now(
        "/willow/goal", "omniplanner_msgs/msg/ConstrainedPddlGoalMsg", fields
    )
    assert len(published) == 1
    _check_fields_untouched(fields)


def test_send_keeps_a_msgpack_able_goal_for_the_hub(bridge, monkeypatch):
    """_send → hub.publish (msgpack) of the plan view, as in the HTTP call."""
    rb, published, _ = bridge
    frames = []
    rb.hub = types.SimpleNamespace(
        publish=lambda topic, data: frames.append(
            msgpack.packb(data, use_bin_type=True)
        )
    )
    monkeypatch.setattr(rb, "goal_manager_subscribers", lambda: 1)
    monkeypatch.setattr(rb, "goal_manager_engine", lambda: "fast_downward")
    out = rb.send_pddl_goal(
        "euclid", "(visited-object o2)", constraints=[["use-robots", "euclid"]]
    )
    assert len(published) == 1 and frames
    msgpack.packb(out, use_bin_type=True)
    rb._publish_plan("euclid")  # what _on_plan does later on the executor


# ------------------------------------------------------- a failing callback


def test_a_failing_plan_callback_is_logged_not_raised(caplog):
    rb = RosBridge(types.SimpleNamespace(ros=False, planner_ns="willow"))

    def boom(topic, data):
        raise TypeError("can not serialize 'ConstraintFact' object")

    rb.hub = types.SimpleNamespace(publish=boom)
    rb._on_llm("euclid", types.SimpleNamespace(data="hi"))  # does not raise
    assert "ROS callback _on_llm failed" in caplog.text
    rb._on_repair_alert("euclid", "alert", "x")
    assert "ROS callback _on_repair_alert failed" in caplog.text


def test_spin_goes_on_after_a_callback_exception(caplog):
    """_spin: an exception out of executor.spin() (a callback's) is logged and
    the executor spins again; it returns when spin() returns (shutdown)."""
    rb = RosBridge(types.SimpleNamespace(ros=False, planner_ns="willow"))
    rb.available = True
    calls = []

    def spin():
        calls.append(1)
        if len(calls) < 3:
            raise TypeError("can not serialize 'ConstraintFact' object")

    rb._executor = types.SimpleNamespace(spin=spin)
    rb._spin()
    assert len(calls) == 3
    assert "the executor spins on" in caplog.text
    # stopped: an exception ends the loop instead of spinning again
    rb.available = False
    calls.clear()
    rb._executor = types.SimpleNamespace(spin=lambda: (calls.append(1), 1 / 0))
    rb._spin()
    assert len(calls) == 1


def test_real_executor_survives_a_raising_subscription_callback():
    """With rclpy: a subscription callback that raises does not stop _spin, so a
    later call() (how publish / goal_manager_subscribers reach ROS) still runs."""
    try:
        import rclpy
        from rclpy.executors import MultiThreadedExecutor
        from std_msgs.msg import String
    except Exception:  # noqa: BLE001
        pytest.skip("rclpy not importable (source ROS)")
    rb = RosBridge(types.SimpleNamespace(ros=False, planner_ns="willow"))
    rb._context = rclpy.Context()
    rclpy.init(context=rb._context, domain_id=97)
    try:
        rb.node = rclpy.create_node("adt4_console_spin_test", context=rb._context)
        rb._guard = rb.node.create_guard_condition(rb._drain)
        rb._executor = MultiThreadedExecutor(num_threads=2, context=rb._context)
        rb._executor.add_node(rb.node)
        got = []

        def cb(msg):
            got.append(msg.data)
            raise TypeError("can not serialize 'ConstraintFact' object")

        rb.node.create_subscription(String, "/adt4_spin_test", cb, 10)
        pub = rb.node.create_publisher(String, "/adt4_spin_test", 10)
        rb.available = True
        t = threading.Thread(target=rb._spin, daemon=True)
        t.start()
        for i in range(3):
            pub.publish(String(data=str(i)))
            deadline = time.time() + 5
            while len(got) <= i and time.time() < deadline:
                time.sleep(0.02)
        assert got == ["0", "1", "2"]
        assert t.is_alive()
        assert rb.run(lambda: 42, timeout=5) == 42
    finally:
        rb.available = False
        rb._executor.shutdown(timeout_sec=2)
        rb.node.destroy_node()
        rb._context.try_shutdown()
