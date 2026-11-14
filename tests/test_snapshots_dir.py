"""--snapshots-dir: where Save map file / Map > Snapshots write (default ~/.adt4_console),
and the snapshot info says where its dsg.json is."""

from adt4_console import cli


def test_snapshots_dir_flag():
    assert (
        cli.settings_from_args(cli.parse_args([])).snapshots_dir
        == "~/.adt4_console/snapshots"
    )
    s = cli.settings_from_args(cli.parse_args(["--snapshots-dir", "/data/x/snapshots"]))
    assert s.snapshots_dir == "/data/x/snapshots"


def test_snapshot_info_has_its_paths(tmp_path, monkeypatch):
    from types import SimpleNamespace as NS

    from adt4_console import edit_service

    def export(db, d, include_mesh=False):
        d.mkdir(parents=True)
        (d / "dsg.json").write_text("{}")
        return {"counts": {"Room": 2}, "mesh": include_mesh}

    monkeypatch.setattr(edit_service, "_ge", lambda: NS(export_snapshot=export))
    svc = edit_service.EditService(NS(snapshots_dir=str(tmp_path)), NS(), None, None)
    monkeypatch.setattr(
        svc, "source", lambda sid: NS(name="pennfused", db=lambda: None)
    )
    info = svc.snapshot("neo4j:pennfused", "penn regions", include_mesh=True)
    assert info["dsg"] == info["dir"] + "/dsg.json" and info["dir"].startswith(
        str(tmp_path / "pennfused")
    )
    assert info["mesh"] is True and info["counts"] == {"Room": 2}
