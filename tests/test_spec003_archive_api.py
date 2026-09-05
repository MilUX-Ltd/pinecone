"""Spec 003, criteria 4 and 5, from the outside: the archive says what it holds, and the player can
ask it for a recent window."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DETAIL = '<detail><contact callsign="ALPHA"/><takv platform="ATAK-CIV"/></detail>'


def get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


@pytest.fixture()
def served(tmp_path: Path):
    sys.path.insert(0, str(ROOT))
    import pinecone_archive

    state = tmp_path / "state"
    state.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    a = pinecone_archive.Archive(str(archive_dir / "pinecone.db"))
    now = int(time.time() * 1000)
    rows = []
    for i in range(6):
        t = time.strftime("%Y-%m-%d %H:%M:%S+00", time.gmtime((now - (5 - i) * 60_000) / 1000))
        rows.append(
            {
                "id": i + 1,
                "uid": "ANDROID-1",
                "cot_type": "a-f-G-U-C",
                "how": "m-g",
                "start": t,
                "time": t,
                "stale": t,
                "servertime": t,
                "lat": 51.213 + i * 1e-4,
                "lon": -1.505,
                "point_hae": 95.0,
                "point_ce": 9.0,
                "point_le": 9.0,
                "detail": DETAIL,
            }
        )
    a.record(rows)
    a.close()
    # Without a discovery record the page renders its short form, and every assertion about the
    # full report below it would pass by never being reached.
    disc = tmp_path / "discovery.json"
    disc.write_text(
        json.dumps(
            {
                "tak": {
                    "version": "5.8-RELEASE75",
                    "version_source": "dpkg",
                    "unit": "takserver",
                    "unit_state": "active",
                },
                "ports": [8089, 8443],
                "database": {
                    "host": "127.0.0.1",
                    "port": 5432,
                    "database": "cot",
                    "source": "/opt/tak/CoreConfig.xml",
                    "timezone": "UTC",
                },
                "files": {},
                "retention": {"ttls": {}, "purges": False, "source": "/opt/tak/conf/retention"},
                "rows": None,
                "credential": {
                    "role": "pinecone",
                    "grant": "SELECT on cot_router",
                    "created": True,
                    "statement": "reads with its own role",
                },
                "discovered_at": "2026-09-04T15:00:00Z",
            }
        )
    )
    env = dict(os.environ)
    env["PINECONE_DISCOVERY"] = str(disc)
    port = 9190 + (os.getpid() % 70)
    p = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "serve.py"),
            "--port",
            str(port),
            "--data",
            str(data),
            "--state",
            str(state),
            "--archive",
            str(archive_dir / "pinecone.db"),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/version", timeout=1).read()
                break
            except Exception:
                time.sleep(0.1)
        yield f"http://127.0.0.1:{port}"
    finally:
        p.terminate()
        p.wait(timeout=5)


def test_the_archive_reports_what_it_holds(served: str) -> None:
    code, body = get(f"{served}/api/archive")
    assert code == 200
    d = json.loads(body)
    assert d["count"] == 6
    assert d["first"] and d["last"]
    assert isinstance(d["free_bytes"], int) and d["free_bytes"] > 0
    assert d["recording"] in (True, False)
    code, html = get(f"{served}/status")
    assert code == 200
    # `"6" in html` used to stand for the count, and the fixture's discovered_at date supplies a
    # 6 on its own, so it proved nothing. Ask for the count where the count is actually rendered.
    assert "<b>6</b> reports recorded" in html
    assert str(d["path"]) in html


def test_the_player_can_ask_for_a_recent_window(served: str) -> None:
    code, body = get(f"{served}/bundles")
    assert code == 200
    names = [b["name"] for b in json.loads(body)]
    assert any(n.startswith("archive:") for n in names), "the archive's recent windows are offered"
    window = next(n for n in names if n.startswith("archive:"))
    code, body = get(f"{served}/bundle.json?name={window}")
    assert code == 200
    b = json.loads(body)
    assert b["format"] == "pinecone-bundle/0"
    assert b["counts"]["tracks"] == 1
    assert b["tracks"][0]["callsign"] == "ALPHA"
    assert b["tracks"][0]["n"] == 6


def test_an_archive_that_is_not_there_is_said_so_not_guessed(tmp_path: Path) -> None:
    port = 9270 + (os.getpid() % 60)
    data = tmp_path / "d"
    data.mkdir()
    p = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "serve.py"),
            "--port",
            str(port),
            "--data",
            str(data),
            "--archive",
            str(tmp_path / "missing.db"),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/version", timeout=1).read()
                break
            except Exception:
                time.sleep(0.1)
        code, body = get(f"http://127.0.0.1:{port}/api/archive")
        assert code == 200
        d = json.loads(body)
        assert d["count"] == 0 and d["recording"] is False
        assert d["reason"]
    finally:
        p.terminate()
        p.wait(timeout=5)


# The two gating findings the pre-UAT review saw on the live page rather than in the tests.


def test_the_page_does_not_contradict_the_record_it_just_showed(served: str) -> None:
    """Criterion 5. The page carried a line from the previous slice, so it read '6 reports
    recorded ... recording' and then, directly underneath, 'Nothing is recorded yet'."""
    code, body = get(f"{served}/status")
    assert code == 200
    assert "<dt>Exposure</dt>" in body, "this is the full report, not the short page"
    assert "reports recorded" in body
    assert "Nothing is recorded yet" not in body
    assert "arrive with slice 1" not in body


def test_the_page_says_when_the_recorder_last_checked_in(served: str) -> None:
    """Criterion 5, 'when it last ran'. The archive in this fixture was written directly, with no
    recorder behind it, so the page must say the recorder has never checked in rather than implying
    a healthy one from the fact that reports exist."""
    code, body = get(f"{served}/status")
    assert code == 200
    assert "never checked in" in body
    code, body = get(f"{served}/api/archive")
    assert json.loads(body)["last_checked"] == ""


def test_each_offered_window_reports_its_own_count(served: str) -> None:
    """The picker showed the size of the whole archive against all three windows, which told the
    operator nothing about which of them had anything in it."""
    code, body = get(f"{served}/bundles")
    assert code == 200
    windows = [b for b in json.loads(body) if b["name"].startswith("archive:")]
    assert len(windows) == 3
    assert all("reports" in w for w in windows)
    assert windows[0]["reports"] == 6, "the last hour holds the six reports the fixture recorded"
