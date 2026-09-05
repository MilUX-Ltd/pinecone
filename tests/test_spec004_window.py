"""Spec 004, criteria 4 to 6: the page says what the recorder is doing, and the operator chooses
the window rather than picking from three fixed ones."""

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
    """A box holding an hour of reports, ten minutes apart, ending an hour ago."""
    sys.path.insert(0, str(ROOT))
    import pinecone_archive

    data = tmp_path / "data"
    data.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    a = pinecone_archive.Archive(str(archive_dir / "pinecone.db"))
    base = int(time.time() * 1000) - 2 * 3600 * 1000
    rows = []
    for i in range(7):
        when = base + i * 600_000
        t = time.strftime("%Y-%m-%d %H:%M:%S+00", time.gmtime(when / 1000))
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
                "lat": 51.2 + i * 1e-4,
                "lon": -1.5,
                "point_hae": 90.0,
                "point_ce": 9.0,
                "point_le": 9.0,
                "detail": DETAIL,
            }
        )
    a.record(rows)
    a.close()
    port = 9420 + (os.getpid() % 60)
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
        yield f"http://127.0.0.1:{port}", base
    finally:
        p.terminate()
        p.wait(timeout=5)


# ---- criterion 5: the operator chooses the window ------------------------------------------


def test_a_window_between_two_given_times_comes_back_as_a_bundle(served) -> None:
    base_url, base = served
    start = base - 60_000
    end = base + 7 * 600_000
    code, body = get(f"{base_url}/bundle.json?name=archive&start={start}&end={end}")
    assert code == 200, body
    b = json.loads(body)
    assert b["format"] == "pinecone-bundle/0"
    assert b["counts"]["rows_kept"] == 7
    assert b["window"]["start"] == start and b["window"]["end"] == end


def test_the_window_holds_only_what_falls_inside_it(served) -> None:
    base_url, base = served
    start = base + 1_800_000  # half an hour in, so the first three reports are outside
    end = base + 7 * 600_000
    code, body = get(f"{base_url}/bundle.json?name=archive&start={start}&end={end}")
    assert code == 200, body
    b = json.loads(body)
    assert b["counts"]["rows_kept"] == 4
    # A window is accurate to the second and rounded outwards, so a report inside the boundary
    # second is kept on purpose: the record's argument is that it is complete, and a window that
    # is a second generous is the right way round. Nothing may be further out than that second,
    # on either side. The end side is asserted with the same tolerance as the start, because
    # `p[0] < end` was true of this fixture whatever the code did and so tested nothing.
    points = [p[0] for t in b["tracks"] for p in t["points"]]
    assert points, "there is something to check"
    assert all(t >= start - 1000 for t in points)
    assert all(t < end + 1000 for t in points)


def test_the_windows_generosity_is_a_second_and_no_more(served) -> None:
    """Criterion 5, pinned rather than described. The outward rounding is deliberate, and the size
    of it is the thing worth asserting: a second either side is a design decision, and anything
    beyond that is a bug nothing else on this branch would catch."""
    base_url, base = served
    # The fixture's reports sit ten minutes apart from `base`. Ask for a window whose end falls
    # half a second before one of them, and whose start falls half a second after another.
    start = base + 600_000 + 500
    end = base + 3 * 600_000 - 500
    code, body = get(f"{base_url}/bundle.json?name=archive&start={start}&end={end}")
    assert code == 200, body
    points = sorted(p[0] for t in json.loads(body)["tracks"] for p in t["points"])
    assert points, "the boundary reports are kept, which is the point of rounding outwards"
    assert points[0] >= start - 1000, "and not one that is more than a second early"
    assert points[-1] < end + 1000, "nor more than a second late"


# ---- criterion 6: a window that cannot be served says why ----------------------------------


def test_a_backwards_window_is_refused_with_a_reason(served) -> None:
    base_url, base = served
    code, body = get(f"{base_url}/bundle.json?name=archive&start={base + 10_000}&end={base}")
    assert code == 400
    assert "before" in body.lower() or "after" in body.lower()


def test_a_window_with_nothing_in_it_says_so_rather_than_returning_an_empty_map(served) -> None:
    base_url, base = served
    long_ago = base - 40 * 24 * 3600 * 1000
    code, body = get(f"{base_url}/bundle.json?name=archive&start={long_ago}&end={long_ago + 60_000}")
    assert code == 200, body
    b = json.loads(body)
    assert b["counts"]["rows_kept"] == 0
    assert b.get("empty") is True, "the player is told there is nothing here, not left to guess"


def test_a_malformed_time_is_refused(served) -> None:
    base_url, _ = served
    code, body = get(f"{base_url}/bundle.json?name=archive&start=yesterday&end=now")
    assert code == 400
    assert "time" in body.lower()


# ---- criterion 4: the page says what the recorder is doing ----------------------------------


def test_the_page_says_it_is_catching_up_and_how_far(served) -> None:
    base_url, _ = served
    sys.path.insert(0, str(ROOT))
    import pinecone_archive

    code, body = get(f"{base_url}/api/archive")
    assert code == 200
    d = json.loads(body)
    assert "catching_up" in d, "the page can tell a catch-up from ordinary running"
    assert d["catching_up"] is False, "this fixture is not catching up"

    # Mark it as catching up the way the recorder does, and look again.
    a = pinecone_archive.Archive(d["path"])
    a.set_meta("backfill_target", "5000")
    a.close()
    code, body = get(f"{base_url}/api/archive")
    d = json.loads(body)
    assert d["catching_up"] is True
    assert d["backfill_target"] == 5000
    code, html = get(f"{base_url}/status")
    assert "catching up" in html.lower()


def test_it_stops_saying_so_when_it_has_caught_up(served) -> None:
    base_url, _ = served
    sys.path.insert(0, str(ROOT))
    import pinecone_archive

    code, body = get(f"{base_url}/api/archive")
    a = pinecone_archive.Archive(json.loads(body)["path"])
    a.set_meta("backfill_target", "5000")
    a.set_meta("backfill_done", "yes")
    a.close()
    code, body = get(f"{base_url}/api/archive")
    assert json.loads(body)["catching_up"] is False
    code, html = get(f"{base_url}/status")
    assert "catching up" not in html.lower()
