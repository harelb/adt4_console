"""Launch: a robot session must match the launch mode (mapping vs relocalize).

2026-11-11: a relocalize launch from willow ran spot-indoor_image_logging (a
mapping session, remembered from the previous launch), which ignores the prior
map, so no robot could relocalize."""

import pytest

from adt4_console.routes import launch as launch_routes

from .conftest import needs_fleet_core

# the plan the field run sent (launch_memory.json), minus the machines we have here
FIELD_RUN = {
    "robots": ["euclid"],
    "session": "spot-indoor_image_logging",
    "experiment": "nov11_b45_v3_map_images_v2",
    "mode": "relocalize",
    "prior_path": "/home/rrg/adt4_output/nov11_b45_v3_map_images__euclid",
    "prior_source": "willow",
    "record": True,
    "base_station": "willow",
    "bs_session": "base_station_web_repair-default",
    "sessions": {},
    "base_only": False,
    "preflight": False,
}


def test_session_mode_by_name():
    m = launch_routes.session_mode
    assert m("spot_relocalize-relocalize") == "relocalize"
    assert m("phoenix_bag_relocalize-relocalize_awcd") == "relocalize"
    assert m("spot-indoor_image_logging") == "mapping"
    assert m("phoenix-default") == "mapping"
    assert m("base_station_web_repair-default") is None
    assert m("spot_prior_dsg-prior_dsg") is None


@needs_fleet_core
def test_defaults_expose_session_modes(client):
    d = client.get("/api/launch/defaults", params={"mode": "relocalize"}).json()
    modes = d["session_modes"]
    assert modes["spot_relocalize-relocalize"] == "relocalize"
    assert modes["spot-indoor_image_logging"] == "mapping"
    assert "base_station_web_repair-default" not in modes
    assert set(modes) <= set(d["sessions"])


@needs_fleet_core
def test_relocalize_with_a_mapping_session_is_refused(client):
    for url in ("/api/launch", "/api/launch/dry_run"):
        r = client.post(url, json=FIELD_RUN)
        assert r.status_code == 400, url
        detail = r.json()["detail"]
        assert "euclid" in detail and "spot-indoor_image_logging" in detail and "mapping session" in detail
    ok = {**FIELD_RUN, "session": "spot_relocalize-relocalize"}
    assert client.post("/api/launch/dry_run", json=ok).status_code == 200


@needs_fleet_core
def test_mapping_with_a_relocalize_session_is_refused(client):
    plan = {**FIELD_RUN, "mode": "mapping", "robots": ["euclid", "gauss"], "session": "",
            "sessions": {"spot": "spot-default", "phoenix": "phoenix_relocalize-relocalize"}}  # fmt: skip
    r = client.post("/api/launch", json=plan)
    assert r.status_code == 400
    assert "gauss" in r.json()["detail"] and "relocalize session" in r.json()["detail"]
    assert "euclid" not in r.json()["detail"]


@needs_fleet_core
def test_relocalize_still_needs_a_prior(client):
    plan = {**FIELD_RUN, "session": "spot_relocalize-relocalize", "prior_path": "", "prior_source": None}
    r = client.post("/api/launch", json=plan)
    assert r.status_code == 400 and "prior" in r.json()["detail"]


@needs_fleet_core
def test_fallback_check_without_fleet_tooling_support(client, monkeypatch):
    """Fleet tooling without session_mode / check_session_modes: the console's
    name-based rule still refuses the mismatch."""
    from dcist_launch_system.fleet import launch_workflow as lw

    monkeypatch.delattr(lw, "session_mode", raising=False)
    monkeypatch.delattr(lw, "check_session_modes", raising=False)
    # validate_plan would refuse it too; the console's own check must, without it
    monkeypatch.setattr(lw, "validate_plan", lambda *a, **k: [])
    r = client.post("/api/launch", json=FIELD_RUN)
    assert r.status_code == 400 and "mapping session" in r.json()["detail"]
    modes = client.get("/api/launch/defaults", params={"mode": "relocalize"}).json()["session_modes"]
    assert modes["spot-indoor_image_logging"] == "mapping"
    assert modes["spot_relocalize-relocalize"] == "relocalize"
