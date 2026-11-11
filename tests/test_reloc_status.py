"""Relocalization status (robot test 2026-11-11): only robots on the team are
listed, and the not-yet-matched wording names no robot."""

from adt4_console.graph_service import GraphService
from adt4_console.routes.change_detection import relocalized

FA_OK = {
    "parent": "map",
    "child": "euclid/odom",
    "ok": True,
    "T": [0, 0, 0, 1, 0, 0, 0],
}


class _Live:
    """fleetlink knows euclid + hamilton (today's team); gauss is in the topology only."""

    store_root = None
    receiver = object()

    def robots(self):
        return ["euclid", "hamilton"]


def test_graph_sources_list_only_live_team_robots(settings):
    g = GraphService(settings, live=_Live())
    g.live_source("gauss")  # a viewer asked for live:gauss (not running today)
    ids = {s["id"] for s in g.list()}
    assert {"live:euclid", "live:hamilton"} <= ids
    assert "live:gauss" not in ids
    assert g.get("live:gauss").name == "gauss"  # still resolvable, just not listed


def test_no_prior_match_reason_is_unambiguous():
    r = relocalized({"frame_align": FA_OK, "reloc": {"prior_lcs": 0}})
    assert not r["ok"]
    assert "no prior-map match yet" in r["reason"]
    assert "guess" not in r["reason"]  # "starting guess" was read as "starting gauss"
