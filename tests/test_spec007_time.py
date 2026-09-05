"""Spec 007, honest time: latency, clock disagreement and dropouts are measured from what the
record already holds, and reported rather than pretended away. All synthetic."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DETAIL = '<detail><contact callsign="{cs}"/><takv platform="ATAK-CIV"/></detail>'


def stamp(ms: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ms / 1000)) + f".{ms % 1000:03d}+00"


def row(i: int, cs: str, server_ms: int, device_ms: int | None) -> dict:
    return {
        "id": i,
        "uid": f"UID-{cs}",
        "cot_type": "a-f-G-U-C",
        "how": "m-g",
        "start": stamp(device_ms) if device_ms is not None else "",
        "time": stamp(device_ms) if device_ms is not None else "",
        "stale": stamp(server_ms + 60_000),
        "servertime": stamp(server_ms),
        "lat": 51.2 + i * 1e-5,
        "lon": -1.5,
        "point_hae": 90.0,
        "point_ce": 9.0,
        "point_le": 9.0,
        "detail": DETAIL.format(cs=cs),
    }


T0 = 1_788_426_000_000


def bundle_of(rows: list[dict]) -> dict:
    import build_bundle

    return build_bundle.bundle_from_rows(rows, start_ms=T0 - 1, end_ms=T0 + 10**8)


def track(b: dict, cs: str) -> dict:
    return next(t for t in b["tracks"] if t["callsign"] == cs)


# ---- criteria 1 and 2: latency and clock disagreement -----------------------------------------


def test_latency_is_measured_per_callsign() -> None:
    # every 30 s, arriving 2 s late, except one that arrived 20 s late
    rows = [row(i, "ALPHA", T0 + i * 30_000 + (20_000 if i == 5 else 2_000), T0 + i * 30_000) for i in range(20)]
    t = track(bundle_of(rows), "ALPHA")["time"]
    assert t["latency_median_ms"] == 2_000
    assert t["latency_max_ms"] == 20_000
    assert 2_000 <= t["latency_p95_ms"] <= 20_000
    assert t["latency_known"] == 20


def test_a_reports_own_latency_is_on_its_point() -> None:
    rows = [row(i, "ALPHA", T0 + i * 30_000 + 3_000, T0 + i * 30_000) for i in range(3)]
    pts = track(bundle_of(rows), "ALPHA")["points"]
    # point layout: [servertime, lat, lon, hae, speed, course, battery, stale, device time, how]
    assert all(p[0] - p[8] == 3_000 for p in pts), "a report's own latency is its server time minus its device time"


def test_a_clock_that_runs_ahead_is_reported_not_folded_in() -> None:
    """A device whose clock is ahead sends reports that 'arrive before they were sent'. Folding a
    negative into the latency figures would make a bad clock look like a fast link."""
    rows = [row(i, "BRAVO", T0 + i * 30_000 + 2_000, T0 + i * 30_000) for i in range(10)]
    rows += [row(100 + i, "BRAVO", T0 + 400_000 + i * 30_000, T0 + 400_000 + i * 30_000 + 45_000) for i in range(3)]
    t = track(bundle_of(rows), "BRAVO")["time"]
    assert t["clock_ahead_count"] == 3
    assert t["clock_ahead_max_ms"] == 45_000
    assert t["latency_median_ms"] == 2_000, "the three bad-clock reports did not drag the median below the truth"
    assert t["latency_known"] == 10


# ---- criterion 3: dropouts ------------------------------------------------------------------------


def test_dropouts_are_listed_with_the_threshold_that_found_them() -> None:
    # every 30 s for ten minutes, then nothing for twenty minutes, then ten more minutes
    a = [row(i, "CHARLIE", T0 + i * 30_000, T0 + i * 30_000) for i in range(20)]
    b = [row(100 + i, "CHARLIE", T0 + 30 * 60_000 + i * 30_000, T0 + 30 * 60_000 + i * 30_000) for i in range(20)]
    t = track(bundle_of(a + b), "CHARLIE")["time"]
    assert t["dropout_threshold_ms"] == 4 * 30_000, "four times the median interval, as the player already uses"
    assert len(t["dropouts"]) == 1
    d = t["dropouts"][0]
    assert d["from"] == T0 + 19 * 30_000 and d["to"] == T0 + 30 * 60_000
    assert d["ms"] == d["to"] - d["from"]


def test_missing_time_is_the_sum_of_the_dropouts() -> None:
    a = [row(i, "DELTA", T0 + i * 30_000, T0 + i * 30_000) for i in range(10)]
    b = [row(100 + i, "DELTA", T0 + 20 * 60_000 + i * 30_000, T0 + 20 * 60_000 + i * 30_000) for i in range(10)]
    c = [row(200 + i, "DELTA", T0 + 60 * 60_000 + i * 30_000, T0 + 60 * 60_000 + i * 30_000) for i in range(10)]
    t = track(bundle_of(a + b + c), "DELTA")["time"]
    assert len(t["dropouts"]) == 2
    assert t["missing_ms"] == sum(d["ms"] for d in t["dropouts"])


# ---- criterion 4: unknown is unknown ------------------------------------------------------------


def test_no_device_time_means_unknown_not_zero() -> None:
    rows = [row(i, "ECHO", T0 + i * 30_000, None) for i in range(5)]
    t = track(bundle_of(rows), "ECHO")["time"]
    assert t["latency_known"] == 0
    assert t["latency_median_ms"] is None, "unknown, not zero: zero would be a claim"


# ---- criterion 5: it reaches the page ---------------------------------------------------------------


@pytest.fixture()
def served(tmp_path: Path):
    import pinecone_archive

    for d in ("data", "state", "archive"):
        (tmp_path / d).mkdir()
    a = pinecone_archive.Archive(str(tmp_path / "archive" / "pinecone.db"))
    now = int(time.time() * 1000) - 3600_000
    a.record([row(i + 1, "FOX", now + i * 30_000 + 1_500, now + i * 30_000) for i in range(10)])
    a.close()
    port = 9760 + (os.getpid() % 40)
    p = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "serve.py"),
            "--port",
            str(port),
            "--data",
            str(tmp_path / "data"),
            "--state",
            str(tmp_path / "state"),
            "--archive",
            str(tmp_path / "archive" / "pinecone.db"),
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
        yield f"http://127.0.0.1:{port}"
    finally:
        p.terminate()
        p.wait(timeout=5)


def test_the_time_facts_reach_the_bundle_the_player_reads(served: str) -> None:
    with urllib.request.urlopen(f"{served}/bundle.json?name=archive:last-6h", timeout=5) as r:
        b = json.load(r)
    t = track(b, "FOX")["time"]
    assert t["latency_median_ms"] == 1_500
    assert "dropouts" in t and "dropout_threshold_ms" in t
    assert b["time"][
        "note"
    ], "the bundle says what the record holds: what the server received, not what a handset showed"


def test_a_bursty_handset_is_not_reported_as_dropping_out_between_breaths() -> None:
    """ATAK reports in bursts with a median interval under a second. Four times that is a few
    seconds, and calling every pause between bursts a dropout would bury the real ones. The
    player always clamped the threshold to at least ninety seconds, and the record does too; and
    since a real box showed a handset sending pairs of events in one instant every three minutes,
    events under a second apart are one report for the cadence, so a bursty handset's cadence is
    the breath between bursts, not the tick within one (amended 5 September 2026, declared)."""
    rows = []
    n = 0
    for burst in range(6):  # six bursts a minute apart, each ten reports 600 ms apart
        for i in range(10):
            rows.append(row(n, "GOLF", T0 + burst * 60_000 + i * 600, T0 + burst * 60_000 + i * 600))
            n += 1
    t = track(bundle_of(rows), "GOLF")["time"]
    breath = 60_000 - 9 * 600  # from the last report of one burst to the first of the next
    assert t["dropout_threshold_ms"] == 4 * breath, "four times the breath, never four times 600 ms"
    assert t["dropout_threshold_ms"] >= 90_000, "and never below the floor"
    assert t["dropouts"] == [], "a minute between bursts is not a dropout"


def test_the_threshold_never_exceeds_an_hour() -> None:
    rows = [row(i, "HOTEL", T0 + i * 3_600_000, T0 + i * 3_600_000) for i in range(4)]  # hourly
    t = track(bundle_of(rows), "HOTEL")["time"]
    assert t["dropout_threshold_ms"] == 3_600_000


# From the review: two mutations survived the suite, so it could not tell the ninety-fifth
# percentile from the maximum or from the median, and could not tell > from >= at the boundary.


def test_the_ninety_fifth_percentile_is_neither_the_median_nor_the_worst() -> None:
    """Twenty reports two seconds late, one twenty seconds late, one sixty seconds late. The
    median is 2 s, the worst is 60 s, and the ninety-fifth percentile sits at the smaller outlier."""
    lat = [2_000] * 20 + [20_000, 60_000]
    rows = [row(i, "INDIA", T0 + i * 30_000 + lat[i], T0 + i * 30_000) for i in range(len(lat))]
    t = track(bundle_of(rows), "INDIA")["time"]
    assert t["latency_median_ms"] == 2_000
    assert t["latency_max_ms"] == 60_000
    assert t["latency_p95_ms"] == 20_000, "not the median, not the worst"


def test_a_gap_exactly_at_the_threshold_is_not_a_dropout() -> None:
    """The record and the player both use a strict greater-than, and a mutation to >= survived."""
    a = [row(i, "JULIET", T0 + i * 30_000, T0 + i * 30_000) for i in range(10)]
    last = T0 + 9 * 30_000
    b = [row(100 + i, "JULIET", last + 120_000 + i * 30_000, last + 120_000 + i * 30_000) for i in range(10)]
    t = track(bundle_of(a + b), "JULIET")["time"]
    assert t["dropout_threshold_ms"] == 120_000
    assert t["dropouts"] == [], "a gap equal to the threshold is not a dropout"
    c = [row(200 + i, "JULIET", last + 120_001 + i * 30_000, last + 120_001 + i * 30_000) for i in range(3)]
    t2 = track(bundle_of(a + c), "JULIET")["time"]
    assert len(t2["dropouts"]) == 1, "and one millisecond over is"


def test_a_cadence_is_not_the_gap_between_two_events_sent_at_once() -> None:
    """In use, one handset sends two events in the same instant every three minutes. The median
    interval over raw gaps was 586 ms, the threshold fell to the ninety-second floor, and every
    three-minute interval became a dropout: 119 in six hours, and the callsign read as stale two
    minutes after it had reported (found in use, 5 September 2026). Two events in one second are one
    report for the cadence; the cadence is the gap between reports, so three minutes."""
    import build_bundle

    rows = []
    for i in range(10):
        at = T0 + i * 180_000
        rows.append(row(2 * i + 1, "MilUX", at, at))
        rows.append(row(2 * i + 2, "MilUX", at + 300, at + 300))  # the pair, 300 ms later
    b = build_bundle.bundle_from_rows(rows, start_ms=T0 - 1, end_ms=T0 + 10**7)
    t = b["tracks"][0]
    assert abs(t["median_interval_ms"] - 180_000) < 1_000, t["median_interval_ms"]
    assert t["time"]["dropout_threshold_ms"] == 4 * t["median_interval_ms"]
    assert t["time"]["dropouts"] == [], "a regular three-minute cadence has no dropouts"
