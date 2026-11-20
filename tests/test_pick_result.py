"""Pick results: the robot's "did I pick it up?" question, its images, the
operator's answer on the wire, the HTTP routes and the bridge hook."""

import base64
import io
import json
from types import SimpleNamespace as Obj

import pytest
from PIL import Image

from adt4_console.manipulation_service import ApprovalError
from adt4_console.pick_result_service import (
    HUB_TOPIC,
    PickResultService,
    request_topic,
    response_topic,
    ros_answer_publisher,
)
from adt4_console.ros_bridge import RosBridge
from tests.conftest import make_client


def jpeg(width=4, height=3, kind="JPEG"):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(buffer, format=kind)
    return base64.b64encode(buffer.getvalue()).decode()


def question(request_id="q1", **fields):
    message = {
        "request_id": request_id,
        "object_id": "o58",
        "object_class": "bag",
        "attempt": 1,
        "gripper_holding": True,
        "images": [
            {"name": "hand_color_image", "jpeg_b64": jpeg()},
            {"name": "frontleft_fisheye_image", "jpeg_b64": jpeg(6, 5)},
        ],
    }
    message.update(fields)
    return json.dumps(message)


class Hub:
    def __init__(self):
        self.sent = []

    def publish(self, topic, payload):
        self.sent.append((topic, payload))


def make_service(**kwargs):
    sent = []

    def publish(robot, payload):
        sent.append((robot, payload))
        return {"subscribers": 1}

    clock = Obj(now=1000.0)
    svc = PickResultService(
        kwargs.pop("publish", publish), clock=lambda: clock.now, **kwargs
    )
    svc.sent, svc.test_clock = sent, clock
    return svc


def test_a_question_is_shown_with_its_object_attempt_and_images():
    hub = Hub()
    svc = make_service(hub=hub)
    svc.receive("euclid", question(attempt=3, gripper_holding=False))
    state = svc.pending()
    assert state["available"] is True
    (q,) = state["requests"]
    assert q == {
        "id": "q1",
        "robot": "euclid",
        "object_id": "o58",
        "object_class": "bag",
        "attempt": 3,
        "gripper_holding": False,
        "images": [
            {"name": "hand_color_image", "width": 4, "height": 3},
            {"name": "frontleft_fisheye_image", "width": 6, "height": 5},
        ],
        "received_at": 1000.0,
    }
    data, media = svc.get_image("q1", 1)
    assert media == "image/jpeg" and Image.open(io.BytesIO(data)).size == (6, 5)
    assert hub.sent[-1] == (HUB_TOPIC, state)
    with pytest.raises(ApprovalError) as err:
        svc.get_image("q1", 2)
    assert err.value.status_code == 404


@pytest.mark.parametrize("result", ["picked", "retry", "give_up"])
def test_the_answer_goes_to_the_robot_with_the_questions_id(result):
    hub = Hub()
    svc = make_service(hub=hub)
    svc.receive("euclid", question())
    out = svc.respond("q1", result)
    assert svc.sent == [("euclid", {"request_id": "q1", "result": result})]
    assert out["ok"] and out["robot"] == "euclid" and out["result"] == result
    assert out["object_id"] == "o58" and out["subscribers"] == 1
    assert svc.pending()["requests"] == []
    assert hub.sent[-1][1]["requests"] == []
    # a second click (or another browser) finds nothing to answer
    with pytest.raises(ApprovalError) as err:
        svc.respond("q1", result)
    assert err.value.status_code == 404
    assert len(svc.sent) == 1


def test_an_unknown_result_is_refused_and_the_question_stays():
    svc = make_service()
    svc.receive("euclid", question())
    with pytest.raises(ApprovalError) as err:
        svc.respond("q1", "maybe")
    assert err.value.status_code == 422
    assert svc.sent == [] and len(svc.pending()["requests"]) == 1


def test_an_answer_nobody_received_leaves_the_question_open():
    def nobody(robot, payload):
        raise ApprovalError(503, "not listening")

    svc = make_service(publish=nobody)
    svc.receive("euclid", question())
    with pytest.raises(ApprovalError):
        svc.respond("q1", "retry")
    assert [q["id"] for q in svc.pending()["requests"]] == ["q1"]


def test_the_robots_repeat_of_an_open_question_changes_nothing():
    hub = Hub()
    svc = make_service(hub=hub)
    svc.receive("euclid", question())
    svc.test_clock.now += 20
    svc.receive("euclid", question())
    assert len(hub.sent) == 1
    assert svc.pending()["requests"][0]["received_at"] == 1000.0


def test_a_repeat_right_after_the_answer_is_ignored_but_a_later_one_reopens():
    """The repeat crossed the answer on the way; a much later one means the
    answer never arrived."""
    svc = make_service()
    svc.receive("euclid", question())
    svc.respond("q1", "picked")
    svc.test_clock.now += 2
    svc.receive("euclid", question())
    assert svc.pending()["requests"] == []
    svc.test_clock.now += 30
    svc.receive("euclid", question())
    assert [q["id"] for q in svc.pending()["requests"]] == ["q1"]


def test_the_robot_closing_its_question_removes_it():
    hub = Hub()
    svc = make_service(hub=hub)
    svc.receive("euclid", question())
    svc.receive("euclid", json.dumps({"request_id": "other", "closed": "stopped"}))
    assert len(svc.pending()["requests"]) == 1
    svc.receive("euclid", json.dumps({"request_id": "q1", "closed": "stopped"}))
    assert svc.pending()["requests"] == []
    assert hub.sent[-1][1]["requests"] == []


def test_each_robot_has_its_own_question_and_a_new_one_replaces_the_old():
    svc = make_service()
    svc.receive("euclid", question("q1"))
    svc.receive("hamilton", question("q2", object_id="o2"))
    svc.test_clock.now += 1
    svc.receive("euclid", question("q3", attempt=2))
    assert sorted((q["robot"], q["id"]) for q in svc.pending()["requests"]) == [
        ("euclid", "q3"),
        ("hamilton", "q2"),
    ]
    with pytest.raises(ApprovalError):
        svc.respond("q1", "picked")
    svc.respond("q2", "give_up")
    assert svc.sent == [("hamilton", {"request_id": "q2", "result": "give_up"})]


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        json.dumps({"object_id": "o1"}),
        json.dumps({"request_id": ""}),
        json.dumps({"request_id": 7}),
        question(images=[{"name": "hand", "jpeg_b64": "!!!"}]),
        question(images=[{"name": "hand", "jpeg_b64": jpeg(kind="PNG")}]),
        question(images=[{"name": "hand", "jpeg_b64": jpeg()}] * 7),
        question(images="nope"),
    ],
)
def test_unusable_questions_are_ignored(text):
    svc = make_service()
    svc.receive("euclid", text)
    assert svc.pending()["requests"] == []


def test_a_question_without_images_is_still_shown():
    svc = make_service()
    svc.receive("euclid", question(images=[], gripper_holding=None))
    (q,) = svc.pending()["requests"]
    assert q["images"] == [] and q["gripper_holding"] is None


# --------------------------------------------------------------- transport
class FakeRos:
    def __init__(self, out):
        self.out, self.calls = out, []

    def publish_to_robot(self, robot, topic, msg_type, fields):
        self.calls.append((robot, topic, msg_type, fields))
        return dict(self.out)


def test_the_answer_is_one_json_string_on_the_robots_topic():
    ros = FakeRos({"subscribers": 1})
    details = ros_answer_publisher(ros)(
        "euclid", {"request_id": "q1", "result": "retry"}
    )
    assert details == {"subscribers": 1}
    ((robot, topic, msg_type, fields),) = ros.calls
    assert (robot, topic, msg_type) == (
        "euclid",
        "/euclid/spot_executor_node/pick_result",
        "std_msgs/msg/String",
    )
    assert json.loads(fields["data"]) == {"request_id": "q1", "result": "retry"}
    assert response_topic("euclid") == topic
    assert request_topic("euclid") == "/euclid/spot_executor_node/pick_result_request"


@pytest.mark.parametrize(
    "out",
    [
        {"subscribers": 0},
        {"via": "fleetlink", "error": "topic not relayed", "delivered": False},
    ],
)
def test_an_answer_that_did_not_arrive_is_a_503(out):
    with pytest.raises(ApprovalError) as err:
        ros_answer_publisher(FakeRos(out))(
            "euclid", {"request_id": "q", "result": "picked"}
        )
    assert err.value.status_code == 503


def test_a_fleetlink_answer_the_robot_acknowledged_is_delivered():
    out = {"via": "fleetlink", "delivered": True, "subscribers": 1}
    assert ros_answer_publisher(FakeRos(out))("euclid", {})["delivered"] is True


# ------------------------------------------------------------------ routes
@pytest.fixture()
def api(settings):
    client, services = make_client(settings)
    services.pick_result = make_service(hub=services.hub)
    return client, services.pick_result


def test_routes_list_serve_images_and_answer(api):
    client, svc = api
    assert client.get("/api/pick_result/pending").json()["requests"] == []
    svc.receive("euclid", question())
    (q,) = client.get("/api/pick_result/pending").json()["requests"]
    base = f"/api/pick_result/requests/{q['id']}"
    image = client.get(f"{base}/images/0")
    assert image.status_code == 200 and image.headers["content-type"] == "image/jpeg"
    assert image.headers["cache-control"] == "no-store"
    assert client.get(f"{base}/images/9").status_code == 404
    assert client.post(f"{base}/respond", json={"result": "maybe"}).status_code == 422
    assert client.post(f"{base}/respond", json={}).status_code == 422
    done = client.post(f"{base}/respond", json={"result": "retry"})
    assert done.status_code == 200 and done.json()["result"] == "retry"
    assert svc.sent == [("euclid", {"request_id": "q1", "result": "retry"})]
    assert client.post(f"{base}/respond", json={"result": "retry"}).status_code == 404
    assert client.get("/api/pick_result/pending").json()["requests"] == []


def test_the_app_wires_the_service_to_the_bridge(settings):
    client, services = make_client(settings)
    assert services.pick_result is not None
    assert services.ros.pick_result is services.pick_result


def test_the_bridge_hands_questions_to_the_service():
    bridge = RosBridge.__new__(RosBridge)
    bridge.pick_result = make_service()
    bridge._on_pick_result_request("euclid", Obj(data=question()))
    assert bridge.pick_result.pending()["requests"][0]["robot"] == "euclid"
    bridge.pick_result.receive = lambda *_: 1 / 0
    bridge._on_pick_result_request(
        "euclid", Obj(data=question("q2"))
    )  # logged, not raised


def test_a_relayed_answer_not_yet_acked_closes_the_question():
    """2026-11-19 and the sim: the robot acted on the answer while the console
    said 503 "try again". A late ack is not a lost answer; a lost one is asked again."""
    publish = ros_answer_publisher(FakeRos({"via": "fleetlink", "delivered": False}))
    assert publish("euclid", {"request_id": "r", "result": "picked"})["unconfirmed"] is True

