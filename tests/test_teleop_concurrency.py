"""A stop completes after pending motion, with zero as the final publication."""

import threading

import pytest

from adt4_console.teleop_service import TeleopService


@pytest.mark.parametrize("operation", ["halt", "release", "zero", "deadman"])
def test_stop_serializes_with_pending_publication(operation):
    entered = threading.Event()
    publish = threading.Event()
    stopping = threading.Event()
    stopped = threading.Event()
    sent = []
    now = [10.0]

    def record(topic, msg_type, fields):
        vx = fields["twist"]["linear"]["x"]
        if vx:
            entered.set()
            assert publish.wait(3)
            if operation == "halt":
                # An arm already dispatching when STOP arrived cannot clear
                # the final latch while STOP waits for this publication.
                svc.rearm("gauss")
        # Publisher callbacks may inspect session state without deadlocking.
        svc.session("gauss")
        svc.stopped("gauss")
        sent.append(vx)

    svc = TeleopService(record, clock=lambda: now[0], platform_of=lambda r: "phoenix")
    commands = []
    driver = threading.Thread(
        target=lambda: commands.append(svc.command("gauss", "driver", 1, 0, 0)),
        daemon=True,
    )

    def stop():
        stopping.set()
        if operation == "halt":
            svc.halt("gauss")
        elif operation == "release":
            svc.release("gauss", "driver")
        elif operation == "zero":
            svc.zero("gauss")
        else:
            now[0] += 0.5
            svc.check_deadman()
        stopped.set()

    stopper = threading.Thread(target=stop, daemon=True)
    driver.start()
    try:
        assert entered.wait(2)
        stopper.start()
        assert stopping.wait(2)
        assert not stopped.wait(0.1), (
            "STOP returned before pending motion was published"
        )
    finally:
        publish.set()
        driver.join(2)
        if stopper.ident is not None:
            stopper.join(2)
    assert not driver.is_alive() and not stopper.is_alive()
    assert commands[0]["ok"] and stopped.is_set()
    assert sent[0] == 1 and sent[-1] == 0
    if operation == "halt":
        assert svc.stopped("gauss")
        assert svc.session("gauss") is None
        assert not svc.command("gauss", "driver", 1, 0, 0)["ok"]
        assert sent[-1] == 0


def test_blocked_robot_does_not_block_another_robots_stop():
    entered = threading.Event()
    publish = threading.Event()
    stopped = threading.Event()

    def record(topic, msg_type, fields):
        if topic.startswith("/gauss/") and fields["twist"]["linear"]["x"]:
            entered.set()
            assert publish.wait(3)

    svc = TeleopService(record, platform_of=lambda r: "phoenix")
    driver = threading.Thread(
        target=lambda: svc.command("gauss", "driver", 1, 0, 0), daemon=True
    )
    stopper = threading.Thread(
        target=lambda: (svc.halt("lewis"), stopped.set()), daemon=True
    )
    driver.start()
    try:
        assert entered.wait(2)
        stopper.start()
        assert stopped.wait(1)
        assert svc.stopped("lewis")
    finally:
        publish.set()
        driver.join(2)
        if stopper.ident is not None:
            stopper.join(2)
    assert not driver.is_alive() and not stopper.is_alive()
