"""Pick results: after a pick Spot believes worked, the operator says whether
the robot really holds the object.

``spot_executor_node`` publishes one JSON question (std_msgs/String) on
``/<robot>/spot_executor_node/pick_result_request`` and waits, with the object
in the gripper, for the JSON answer on ``/<robot>/spot_executor_node/pick_result``:

  question: {"request_id", "object_id", "object_class", "attempt",
             "gripper_holding" (Spot's own sensor, or null),
             "images": [{"name", "jpeg_b64"}]}
  answer:   {"request_id", "result": "picked" | "retry" | "give_up"}
  closing:  {"request_id", "closed": <result or "stopped">} on the question
            topic once the robot stops waiting.

"picked" lets the plan continue, "retry" makes the robot let go and pick again,
"give_up" ends the robot's action sequence.  The robot repeats an unanswered
question with the same ``request_id``; an answer is matched by it, so a stale
browser cannot answer a later pick.  This module keeps the open question of
each robot, serves its images (``routes/pick_result.py``) and publishes the
answer through the bridge.
"""

from __future__ import annotations

import base64
import binascii
import io
import json
import logging
import threading
import time

from PIL import Image

from .manipulation_service import ApprovalError

log = logging.getLogger(__name__)

MESSAGE_TYPE = "std_msgs/msg/String"
HUB_TOPIC = "manipulation/pick_result"
RESULTS = ("picked", "retry", "give_up")
MAX_IMAGES = 6
MAX_PIXELS = 25_000_000
# A repeat of a question that was answered this recently is the robot's
# periodic repeat crossing the answer on the way, not a lost answer.
ANSWER_GRACE_S = 8.0


def request_topic(robot: str) -> str:
    return f"/{robot}/spot_executor_node/pick_result_request"


def response_topic(robot: str) -> str:
    return f"/{robot}/spot_executor_node/pick_result"


def _jpeg(entry) -> tuple[str, bytes, int, int]:
    name = str(entry["name"])
    data = base64.b64decode(entry["jpeg_b64"], validate=True)
    with Image.open(io.BytesIO(data)) as image:
        kind, (width, height) = image.format, image.size
    if kind != "JPEG":
        raise ValueError(f"image {name!r} is {kind}, not JPEG")
    if width <= 0 or height <= 0 or width * height > MAX_PIXELS:
        raise ValueError(f"image {name!r} has unusable dimensions")
    return name, data, width, height


class PickResultService:
    """The open pick question of each robot, and its answer.

    ``publish(robot, payload) -> details`` sends the answer (see
    :func:`ros_answer_publisher`); when it raises, the question stays open."""

    def __init__(
        self,
        publish,
        hub=None,
        *,
        available=lambda: True,
        error=lambda: None,
        clock=time.time,
    ):
        self.publish_hook = publish
        self.hub = hub
        self._available, self._error = available, error
        self.clock = clock
        self._lock = threading.RLock()
        self._open: dict[str, dict] = {}  # robot -> question
        self._answered: dict[str, float] = {}  # request_id -> when

    # ------------------------------------------------------------- state
    @staticmethod
    def _public(question) -> dict:
        return {k: v for k, v in question.items() if not k.startswith("_")}

    def pending(self) -> dict:
        with self._lock:
            requests = sorted(
                (self._public(q) for q in self._open.values()),
                key=lambda q: q["received_at"],
            )
        return {
            "available": bool(self._available()),
            "error": self._error(),
            "requests": requests,
        }

    def _notify(self) -> None:
        if self.hub is None:
            return
        try:
            self.hub.publish(HUB_TOPIC, self.pending())
        except Exception:  # noqa: BLE001
            log.exception("publishing %s", HUB_TOPIC)

    # ---------------------------------------------------------- from robot
    def receive(self, robot: str, text: str) -> None:
        """One message from the robot's question topic (ROS executor thread)."""
        try:
            message = json.loads(text)
            request_id = message["request_id"]
            if not isinstance(request_id, str) or not request_id:
                raise ValueError("request_id must be a non-empty string")
        except (ValueError, TypeError, KeyError) as exc:
            log.warning("ignoring a malformed pick question from %s: %s", robot, exc)
            return
        if "closed" in message:
            self._close(robot, request_id, str(message["closed"]))
            return
        with self._lock:
            current = self._open.get(robot)
            if current is not None and current["id"] == request_id:
                return  # the robot's repeat of the question already shown
            answered = self._answered.get(request_id)
            if answered is not None and self.clock() - answered < ANSWER_GRACE_S:
                return
        try:
            entries = message.get("images") or []
            if not isinstance(entries, list) or len(entries) > MAX_IMAGES:
                raise ValueError(f"at most {MAX_IMAGES} images")
            images = [_jpeg(entry) for entry in entries]
            holding = message.get("gripper_holding")
            question = {
                "id": request_id,
                "robot": robot,
                "object_id": str(message.get("object_id") or ""),
                "object_class": str(message.get("object_class") or ""),
                "attempt": int(message.get("attempt") or 1),
                "gripper_holding": holding if isinstance(holding, bool) else None,
                "images": [
                    {"name": name, "width": width, "height": height}
                    for name, _, width, height in images
                ],
                "received_at": self.clock(),
                "_images": [data for _, data, _, _ in images],
            }
        except (ValueError, TypeError, KeyError, binascii.Error, OSError) as exc:
            log.warning("ignoring an unusable pick question from %s: %s", robot, exc)
            return
        with self._lock:
            self._open[robot] = question
            self._answered.pop(request_id, None)
        log.info(
            "pick question from %s: %s (attempt %d)",
            robot,
            question["object_id"],
            question["attempt"],
        )
        self._notify()

    def _close(self, robot: str, request_id: str, how: str) -> None:
        with self._lock:
            current = self._open.get(robot)
            if current is None or current["id"] != request_id:
                return
            del self._open[robot]
        log.info("pick question from %s closed by the robot: %s", robot, how)
        self._notify()

    # --------------------------------------------------------- to browser
    def _find_locked(self, request_id: str) -> dict:
        for question in self._open.values():
            if question["id"] == request_id:
                return question
        raise ApprovalError(404, "This pick question is no longer open")

    def get_image(self, request_id: str, index: int) -> tuple[bytes, str]:
        with self._lock:
            images = self._find_locked(request_id)["_images"]
            if not 0 <= index < len(images):
                raise ApprovalError(404, "No such image")
            return images[index], "image/jpeg"

    def respond(self, request_id: str, result: str) -> dict:
        if result not in RESULTS:
            raise ApprovalError(422, f"result must be one of {', '.join(RESULTS)}")
        with self._lock:
            question = self._find_locked(request_id)
            robot = question["robot"]
            # raises ApprovalError when nothing could have reached the robot:
            # the question then stays open
            details = self.publish_hook(
                robot, {"request_id": request_id, "result": result}
            )
            del self._open[robot]
            self._answered[request_id] = self.clock()
            if len(self._answered) > 200:
                for key in sorted(self._answered, key=self._answered.get)[:100]:
                    del self._answered[key]
        log.info(
            "pick question from %s (%s) answered: %s",
            robot,
            question["object_id"],
            result,
        )
        self._notify()
        return {
            "ok": True,
            "request_id": request_id,
            "robot": robot,
            "object_id": question["object_id"],
            "result": result,
            **(details or {}),
        }


def ros_answer_publisher(ros):
    """``publish(robot, payload)`` that sends the answer through
    :class:`RosBridge` (``publish_to_robot``).  Nothing could have received it
    (no subscriber, or fleetlink could not relay it): 503, and the question
    stays open.  Relayed but not acked in time: answered, ``unconfirmed``.  The robot ignores a second answer to a question it
    has stopped waiting on, so answering again is safe."""

    def publish(robot: str, payload: dict) -> dict:
        out = ros.publish_to_robot(
            robot, response_topic(robot), MESSAGE_TYPE, {"data": json.dumps(payload)}
        )
        details = {
            k: out[k] for k in ("subscribers", "via", "delivered", "link") if k in out
        }
        if out.get("via") == "fleetlink":
            if out.get("error"):
                raise ApprovalError(
                    503, f"Answer not relayed to {robot}: {out['error']}"
                )
            if not out.get("delivered"):
                # No ack in time is not "not received": on 2026-11-19 and in the sim
                # the robot acted on every such answer while the console said 503
                # "try again". The relay keeps retrying, and a robot that really
                # missed it asks again (every pick_result_repeat_s = 20 s, past
                # ANSWER_GRACE_S), so the question is closed as answered.
                log.warning(
                    "pick answer to %s sent, not yet acknowledged by the relay", robot
                )
                details["unconfirmed"] = True
        elif not out.get("subscribers"):
            raise ApprovalError(
                503, f"{robot}'s executor is not listening for the answer"
            )
        return details

    return publish
