"""Playwright test of the Missions page on the synthetic fixture mission (C1/C2).

Builds and collects ``mission_fixture``, serves the real console app on it and
runs ``frontend/tests/missions-browser.mjs``.  Skipped without node, the
built web UI (``adt4_console/static``), playwright-core or a Chromium.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import socket
import subprocess
import threading
import time

import pytest

from tests import mission_fixture as mf

HERE = pathlib.Path(__file__).resolve().parent
FRONT = HERE.parent / "frontend"
STATIC = HERE.parent / "adt4_console" / "static"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.skipif(shutil.which("node") is None, reason="no node")
@pytest.mark.skipif(not (STATIC / "index.html").exists(), reason="web UI not built (npm run build)")
@pytest.mark.skipif(not (FRONT / "node_modules" / "playwright-core").exists(), reason="no playwright-core (npm ci)")
def test_missions_page_in_a_browser(tmp_path, settings):
    import uvicorn

    from adt4_console.app import build_services, create_app
    from tests.conftest import fake_fleet_deps

    fx = mf.make(tmp_path / "fx")
    mf.collect(fx)
    settings.output_root = str(fx["willow"] / "adt4_output")
    settings.static_dir = STATIC
    svc = build_services(settings, fleet_deps=fake_fleet_deps())
    svc.collector = mf.collector(fx)
    app = create_app(settings, svc)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    t0 = time.time()
    while not server.started and time.time() - t0 < 20:
        time.sleep(0.05)
    try:
        shots = os.environ.get("MISSIONS_SHOT_DIR") or str(tmp_path / "shots")
        r = subprocess.run(
            ["node", "tests/missions-browser.mjs"], cwd=FRONT, capture_output=True, text=True, timeout=180,
            env={**os.environ, "CONSOLE_URL": f"http://127.0.0.1:{port}/", "MISSION_ID": mf.MID, "SHOT_DIR": shots},
        )  # fmt: skip
        if "Executable doesn't exist" in r.stderr or "Failed to launch" in r.stderr:
            pytest.skip(f"no usable Chromium: {r.stderr[-300:]}")
        assert r.returncode == 0, r.stdout + r.stderr
        assert '"ok":true' in r.stdout
    finally:
        server.should_exit = True
        th.join(10)
