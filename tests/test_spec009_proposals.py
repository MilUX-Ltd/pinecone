"""Spec 009, proposals never narration: where was X at T, and the moments the tool noticed, with
their evidence and never a reason. Everything synthetic."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

T0 = 1_788_426_000_000
LAT0, LON0 = 51.2100, -1.4900


def track(cs: str, points: list[tuple[int, float, float]], median_ms: int = 30_000) -> dict[str, Any]:
    pts = [[at, lat, lon, 90.0, None, None, None, at + 60_000, at, "m-g"] for at, lat, lon in points]
    return {
        "uid": f"UID-{cs}",
        "callsign": cs,
        "platform": "ATAK-CIV",
        "n": len(pts),
        "points": pts,
        "median_interval_ms": median_ms,
        "time": {"dropout_threshold_ms": 4 * median_ms, "dropouts": [], "missing_ms": 0},
    }


def walk(
    cs: str, start: int, n: int, lat: float, lon: float, dlat: float = 0.0, dlon: float = 0.0, every: int = 30_000
):
    return track(cs, [(start + i * every, lat + i * dlat, lon + i * dlon) for i in range(n)])


def metres(dlat: float) -> float:
    return dlat * 111_320


# ---- criterion 1: where was X at T -------------------------------------------------------------


def test_where_was_a_callsign_at_a_moment() -> None:
    import pinecone_proposals as pp

    b = {"tracks": [walk("ALPHA", T0, 10, LAT0, LON0, dlat=0.0001)], "chat": []}
    ans = pp.where_was(b, "ALPHA", T0 + 4 * 30_000 + 10_000)
    assert ans["known"] is True
    assert ans["at"] == T0 + 4 * 30_000, "the last report at or before the moment"
    assert ans["age_ms"] == 10_000
    assert ans["stale"] is False
    assert abs(ans["lat"] - (LAT0 + 4 * 0.0001)) < 1e-9


def test_a_stale_answer_says_so() -> None:
    import pinecone_proposals as pp

    b = {"tracks": [walk("ALPHA", T0, 3, LAT0, LON0)], "chat": []}
    ans = pp.where_was(b, "ALPHA", T0 + 60 * 60_000)
    assert ans["known"] is True and ans["stale"] is True
    assert ans["age_ms"] > 4 * 30_000, "older than the staleness threshold, and said to be"


def test_an_unknown_callsign_is_said_to_be_unknown() -> None:
    import pinecone_proposals as pp

    ans = pp.where_was({"tracks": [walk("ALPHA", T0, 3, LAT0, LON0)], "chat": []}, "ZULU", T0)
    assert ans["known"] is False
    assert "ZULU" in ans["message"]


# ---- criterion 2: co-location ---------------------------------------------------------------------


def test_co_location_is_proposed_with_its_evidence() -> None:
    import pinecone_proposals as pp

    a = walk("ALPHA", T0, 20, LAT0, LON0)
    b = walk("BRAVO", T0, 20, LAT0 + 0.0002, LON0)  # about 22 m north, for ten minutes
    props = pp.propose({"tracks": [a, b], "chat": []}, overlays=[])
    co = [p for p in props if p["kind"] == "co-location"]
    assert len(co) == 1
    p = co[0]
    assert set(p["callsigns"]) == {"ALPHA", "BRAVO"}
    assert p["at"] == T0 and p["until"] == T0 + 19 * 30_000
    assert p["evidence"]["closest_m"] < 30
    assert p["evidence"]["for_ms"] >= pp.CO_LOCATION_MS


def test_two_callsigns_apart_are_not_proposed() -> None:
    import pinecone_proposals as pp

    a = walk("ALPHA", T0, 20, LAT0, LON0)
    b = walk("BRAVO", T0, 20, LAT0 + 0.01, LON0)  # about 1.1 km north
    assert [p for p in pp.propose({"tracks": [a, b], "chat": []}, overlays=[]) if p["kind"] == "co-location"] == []


# ---- criterion 3: gaps and silences -------------------------------------------------------------------


def test_a_dropout_is_proposed() -> None:
    import pinecone_proposals as pp

    a = walk("CHARLIE", T0, 5, LAT0, LON0)
    a["time"]["dropouts"] = [{"from": T0 + 4 * 30_000, "to": T0 + 26 * 60_000, "ms": 26 * 60_000 - 4 * 30_000}]
    props = pp.propose({"tracks": [a], "chat": []}, overlays=[])
    gaps = [p for p in props if p["kind"] == "dropout"]
    assert len(gaps) == 1 and gaps[0]["callsigns"] == ["CHARLIE"]
    assert gaps[0]["at"] == T0 + 4 * 30_000 and gaps[0]["until"] == T0 + 26 * 60_000


def test_a_silence_across_the_net_is_proposed() -> None:
    import pinecone_proposals as pp

    a = walk("ALPHA", T0, 5, LAT0, LON0)  # two minutes of reports
    b = walk("BRAVO", T0 + 40 * 60_000, 5, LAT0, LON0)  # then nothing from anyone until forty minutes in
    props = pp.propose({"tracks": [a, b], "chat": []}, overlays=[])
    quiet = [p for p in props if p["kind"] == "silence"]
    assert len(quiet) == 1
    assert quiet[0]["at"] == T0 + 4 * 30_000 and quiet[0]["until"] == T0 + 40 * 60_000
    assert quiet[0]["evidence"]["for_ms"] >= pp.SILENCE_MS


# ---- criterion 4: boundary crossings --------------------------------------------------------------------


def test_a_boundary_crossing_is_proposed() -> None:
    import pinecone_proposals as pp

    box = {
        "name": "AO CEDAR",
        "kind": "polygon",
        "reported": False,
        "undated": True,
        # The stride is 0.0005 of latitude; the box's edges sit at 0.00125 and 0.00325 so no report
        # lands on an edge, and the walk enters at the fourth report and leaves at the eighth.
        "coordinates": [
            [LAT0 + 0.00125, LON0 - 0.002],
            [LAT0 + 0.00125, LON0 + 0.002],
            [LAT0 + 0.00325, LON0 + 0.002],
            [LAT0 + 0.00325, LON0 - 0.002],
        ],
    }
    a = walk("DELTA", T0, 10, LAT0, LON0, dlat=0.0005)  # walks north through the box and out the far side
    props = pp.propose({"tracks": [a], "chat": []}, overlays=[{"name": "Boundary", "shapes": [box]}])
    xs = [p for p in props if p["kind"] == "boundary"]
    assert [x["evidence"]["direction"] for x in xs] == ["in", "out"]
    assert all(x["evidence"]["overlay"] == "AO CEDAR" and x["callsigns"] == ["DELTA"] for x in xs)
    assert xs[0]["at"] < xs[1]["at"]
    assert [p for p in pp.propose({"tracks": [a], "chat": []}, overlays=[]) if p["kind"] == "boundary"] == []


# ---- criterion 5: contact and casualty ----------------------------------------------------------------


def test_a_reported_contact_is_proposed_as_reported() -> None:
    import pinecone_proposals as pp

    rc = {
        "name": "REPORTED CONTACT (reported)",
        "label": "REPORTED CONTACT (reported)",
        "kind": "point",
        "reported": True,
        "undated": False,
        "begin_ms": T0 + 5 * 60_000,
        "end_ms": None,
        "coordinates": [[LAT0, LON0]],
        "ce": 250.0,
    }
    props = pp.propose({"tracks": [], "chat": []}, overlays=[{"name": "r.cot", "shapes": [rc]}])
    c = [p for p in props if p["kind"] == "contact"]
    assert len(c) == 1 and c[0]["at"] == T0 + 5 * 60_000
    assert c[0]["evidence"]["reported"] is True, "a reported contact is proposed as reported, never as a fact"


def test_a_message_that_mentions_a_casualty_is_proposed_as_a_message() -> None:
    import pinecone_proposals as pp

    chat = [
        {"sender": "ALPHA", "room": "All Chat Rooms", "text": "moving now", "servertime": T0, "time": T0},
        {
            "sender": "BRAVO",
            "room": "All Chat Rooms",
            "text": "CASEVAC required at the bridge",
            "servertime": T0 + 60_000,
            "time": T0 + 60_000,
        },
    ]
    props = pp.propose({"tracks": [], "chat": chat}, overlays=[])
    m = [p for p in props if p["kind"] == "message"]
    assert len(m) == 1 and m[0]["at"] == T0 + 60_000 and m[0]["callsigns"] == ["BRAVO"]
    assert m[0]["evidence"]["word"].lower() == "casevac"
    assert "text" in m[0]["evidence"], "the message is quoted as a message to look at"


# ---- criterion 6: never narration ------------------------------------------------------------------------


def test_a_proposal_carries_evidence_and_never_a_reason() -> None:
    import pinecone_proposals as pp

    a = walk("ALPHA", T0, 20, LAT0, LON0)
    b = walk("BRAVO", T0, 20, LAT0 + 0.0002, LON0)
    chat = [{"sender": "ALPHA", "room": "x", "text": "contact front", "servertime": T0 + 10_000, "time": T0 + 10_000}]
    props = pp.propose({"tracks": [a, b], "chat": chat}, overlays=[])
    assert props, "there is something to check"
    forbidden = {
        "why",
        "reason",
        "cause",
        "explanation",
        "assessment",
        "rating",
        "score",
        "blame",
        "fault",
        "performance",
    }
    for p in props:
        assert set(p) >= {"id", "kind", "at", "callsigns", "evidence"}
        assert not (set(p) & forbidden)
        assert not (set(p["evidence"]) & forbidden)
        assert isinstance(p["id"], str) and p["id"]
    assert [p["at"] for p in props] == sorted(p["at"] for p in props), "ordered by time"
    assert len(props) <= pp.PROPOSALS_CAP


# ---- criterion 7: dismiss ---------------------------------------------------------------------------------


def test_a_dismissed_proposal_does_not_come_back(tmp_path: Path) -> None:
    import pinecone_proposals as pp

    a = walk("ALPHA", T0, 20, LAT0, LON0)
    b = walk("BRAVO", T0, 20, LAT0 + 0.0002, LON0)
    bundle = {"tracks": [a, b], "chat": []}
    first = pp.propose(bundle, overlays=[])
    assert first
    store = tmp_path / "dismissed.json"
    pp.dismiss(str(store), first[0]["id"])
    again = pp.propose(bundle, overlays=[], dismissed=pp.load_dismissed(str(store)))
    assert first[0]["id"] not in {p["id"] for p in again}
    assert len(again) == len(first) - 1


# ---- criteria 7 and 8: the routes the page reads, and a dismissal kept on the box -----------------------


def call(url: str, data: dict[str, str] | None = None) -> tuple[int, str]:
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def start(port: int, data: Path, state: Path) -> subprocess.Popen:
    p = subprocess.Popen(
        [sys.executable, str(ROOT / "serve.py"), "--port", str(port), "--data", str(data), "--state", str(state)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    for _ in range(60):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/version", timeout=1).read()
            break
        except Exception:
            time.sleep(0.1)
    return p


@pytest.fixture()
def box(tmp_path: Path):
    """A server over one synthetic bundle in which ALPHA and BRAVO walk together twenty metres apart."""
    data = tmp_path / "data"
    data.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    a = walk("ALPHA", T0, 20, LAT0, LON0)
    b = walk("BRAVO", T0, 20, LAT0 + 0.0002, LON0)
    bundle = {
        "format": "pinecone-bundle/0",
        "window": {"start": T0, "end": T0 + 20 * 30_000},
        "tracks": [a, b],
        "chat": [],
        "counts": {"rows_kept": 40},
    }
    (data / "synth.json").write_text(json.dumps(bundle))
    port = 9750 + (os.getpid() % 50)  # a different band from spec 006's fixture
    p = start(port, data, state)
    try:
        yield f"http://127.0.0.1:{port}", state
    finally:
        p.terminate()
        p.wait(timeout=5)


def test_the_proposals_route_answers_for_a_bundle(box) -> None:
    base, _ = box
    code, body = call(f"{base}/api/proposals?name=synth")
    assert code == 200, body
    got = json.loads(body)
    assert got["cap"] > 0
    near = [p for p in got["proposals"] if p["kind"] == "co-location"]
    assert near and near[0]["callsigns"] == ["ALPHA", "BRAVO"]
    assert not any(k in p for p in got["proposals"] for k in ("reason", "cause", "why", "rating", "score"))
    code, body = call(f"{base}/api/proposals?name=no-such-bundle")
    assert code == 404


def test_a_dismissal_is_kept_on_the_box(box) -> None:
    base, state = box
    first = json.loads(call(f"{base}/api/proposals?name=synth")[1])["proposals"]
    assert first
    code, body = call(f"{base}/api/proposals/{first[0]['id']}/dismiss", {})
    assert code == 200, body
    again = json.loads(call(f"{base}/api/proposals?name=synth")[1])
    assert first[0]["id"] not in {p["id"] for p in again["proposals"]}
    assert again["dismissed"] == 1
    assert (state / "dismissed.json").exists()
    code, body = call(f"{base}/api/proposals/..%2Fetc/dismiss", {})
    assert code == 400, "an identity is twelve hex characters, nothing else is written"
    code, body = call(f"{base}/api/proposals/ffffffffffff/dismiss", {})
    assert code == 200, "dismissing what is not proposed is harmless and is not an error"


def test_the_where_route_answers_with_the_age(box) -> None:
    base, _ = box
    at = T0 + 4 * 30_000 + 10_000
    code, body = call(f"{base}/api/where?name=synth&callsign=ALPHA&at={at}")
    assert code == 200, body
    ans = json.loads(body)
    assert ans["known"] is True and ans["age_ms"] == 10_000 and ans["stale"] is False
    ans = json.loads(call(f"{base}/api/where?name=synth&callsign=ZULU&at={at}")[1])
    assert ans["known"] is False and "ZULU" in ans["message"]
    assert call(f"{base}/api/where?name=synth&at={at}")[0] == 400
    assert call(f"{base}/api/where?name=synth&callsign=ALPHA&at=yesterday")[0] == 400


# ---- the slice 6 review: findings and surviving mutants, each held by a test ---------------------------


def test_a_reported_contact_outside_the_window_is_not_proposed() -> None:
    """Review finding G2: a contact dated three weeks before a one-hour window headed the list."""
    import pinecone_proposals as pp

    def rc(at: int, label: str) -> dict[str, Any]:
        return {
            "label": label,
            "kind": "point",
            "reported": True,
            "undated": False,
            "begin_ms": at,
            "end_ms": None,
            "coordinates": [[LAT0, LON0]],
            "ce": 100.0,
        }

    overlays = [
        {"name": "r.cot", "shapes": [rc(T0 - 21 * 86_400_000, "OLD (reported)"), rc(T0 + 5 * 60_000, "NEW (reported)")]}
    ]
    bundle = {"window": {"start": T0, "end": T0 + 60 * 60_000}, "tracks": [], "chat": []}
    labels = [p["evidence"]["label"] for p in pp.propose(bundle, overlays) if p["kind"] == "contact"]
    assert labels == ["NEW (reported)"]
    assert all(T0 <= p["at"] < T0 + 60 * 60_000 for p in pp.propose(bundle, overlays))


def test_where_was_answers_from_the_newest_node_carrying_the_callsign() -> None:
    """Review finding G3: one handset is two nodes with one callsign (an ATAK client and a mesh node); the answer came
    from the older node and said stale while the other had reported ninety seconds earlier."""
    import pinecone_proposals as pp

    atak = walk("MilUX", T0, 4, LAT0, LON0)  # reports for ninety seconds, then nothing
    atak["uid"] = "ANDROID-1"
    mesh = walk("MilUX", T0, 80, LAT0 + 0.001, LON0)  # every thirty seconds for forty minutes
    mesh["uid"] = "!meshnode"
    ans = pp.where_was({"tracks": [atak, mesh], "chat": []}, "MilUX", T0 + 40 * 60_000)
    assert ans["known"] is True and ans["stale"] is False
    assert ans["at"] == T0 + 79 * 30_000 and ans["uid"] == "!meshnode"
    assert ans["nodes"] == 2, "and it says how many nodes carry the name"
    earlier = pp.where_was({"tracks": [mesh, atak], "chat": []}, "MilUX", T0 + 60_000 + 1)
    assert (
        earlier["uid"] in ("ANDROID-1", "!meshnode") and earlier["at"] == T0 + 60_000
    ), "track order does not decide it"


def test_the_two_nodes_of_one_handset_are_not_co_located_with_each_other() -> None:
    import pinecone_proposals as pp

    a = walk("MilUX", T0, 20, LAT0, LON0)
    b = walk("MilUX", T0, 20, LAT0 + 0.0001, LON0)
    b["uid"] = "!meshnode"
    assert [p for p in pp.propose({"tracks": [a, b], "chat": []}, overlays=[]) if p["kind"] == "co-location"] == []


def test_a_silence_counts_a_message_as_signal() -> None:
    """Surviving mutant M9: criterion 3 says no report and no message, and no test had a message."""
    import pinecone_proposals as pp

    a = walk("ALPHA", T0, 3, LAT0, LON0)  # a minute of reports
    b = walk("ALPHA", T0 + 16 * 60_000, 3, LAT0, LON0)  # then sixteen minutes of nothing
    b["uid"] = "UID-ALPHA-2"
    quiet = {"tracks": [a, b], "chat": []}
    assert [p["kind"] for p in pp.propose(quiet, overlays=[])].count("silence") == 1
    spoken = {
        "tracks": [a, b],
        "chat": [
            {
                "uid": "m1",
                "sender": "ALPHA",
                "room": "All Chat Rooms",
                "text": "moving now",
                "servertime": T0 + 8 * 60_000,
                "time": T0 + 8 * 60_000,
            }
        ],
    }
    assert [p["kind"] for p in pp.propose(spoken, overlays=[])].count(
        "silence"
    ) == 0, "a message eight minutes in splits it"


def test_a_dated_polygon_applies_only_inside_its_window() -> None:
    """Surviving mutant M11: no test had a dated polygon."""
    import pinecone_proposals as pp

    box = {
        "name": "AO CEDAR",
        "kind": "polygon",
        "reported": False,
        "undated": False,
        "begin_ms": T0 - 7 * 86_400_000,
        "end_ms": T0 - 6 * 86_400_000,
        "coordinates": [
            [LAT0 + 0.00125, LON0 - 0.002],
            [LAT0 + 0.00125, LON0 + 0.002],
            [LAT0 + 0.00325, LON0 + 0.002],
            [LAT0 + 0.00325, LON0 - 0.002],
        ],
    }
    a = walk("DELTA", T0, 10, LAT0, LON0, dlat=0.0005)
    assert [
        p
        for p in pp.propose({"tracks": [a], "chat": []}, overlays=[{"name": "x", "shapes": [box]}])
        if p["kind"] == "boundary"
    ] == []
    box["begin_ms"], box["end_ms"] = T0 - 60_000, T0 + 86_400_000
    assert (
        len(
            [
                p
                for p in pp.propose({"tracks": [a], "chat": []}, overlays=[{"name": "x", "shapes": [box]}])
                if p["kind"] == "boundary"
            ]
        )
        == 2
    )


def test_proposals_are_in_time_order_and_capped() -> None:
    """Surviving mutants M17 and M18: the fixtures were already in order and under the cap."""
    import pinecone_proposals as pp

    chat = [
        {
            "uid": f"m{i}",
            "sender": "ALPHA",
            "room": "r",
            "text": "casevac requested",
            "servertime": T0 + (300 - i) * 1000,
            "time": T0,
        }
        for i in range(300)
    ]
    props = pp.propose({"tracks": [], "chat": chat}, overlays=[])
    assert len(props) == pp.PROPOSALS_CAP
    assert [p["at"] for p in props] == sorted(p["at"] for p in props)
    assert props[0]["at"] == T0 + 1000, "the earliest first, and the cap takes the latest, not the first found"


def test_watch_words_match_whole_words() -> None:
    """Review note N4: contactless and CONTACTS were proposing."""
    import pinecone_proposals as pp

    def m(i: int, text: str) -> dict[str, Any]:
        return {"uid": f"m{i}", "sender": "ALPHA", "room": "r", "text": text, "servertime": T0 + i * 1000, "time": T0}

    chat = [
        m(1, "contactless payment at the NAAFI"),
        m(2, "CONTACTS list updated"),
        m(3, "CONTACT wait out"),
        m(4, "9 liner to follow"),
        m(5, "Casevac, one times T1"),
    ]
    words = [
        p["evidence"]["word"].lower()
        for p in pp.propose({"tracks": [], "chat": chat}, overlays=[])
        if p["kind"] == "message"
    ]
    assert words == ["contact", "9 liner", "casevac"]


def test_the_route_reads_the_packs_this_box_imported(box) -> None:
    """Surviving mutant M32: the route fixture had no packs, so a route that ignored the overlays
    passed. A pack record is written where import_pack would write it."""
    base, state = box
    pack = state / "packs" / "EX-PACK"
    pack.mkdir(parents=True)
    shape = {
        "kind": "point",
        "label": "ENEMY SECTION (reported)",
        "coordinates": [[LAT0, LON0]],
        "begin_ms": T0 + 5 * 60_000,
        "end_ms": None,
        "undated": False,
        "window_unreadable": False,
        "reported": True,
        "ce": 250.0,
        "remarks": "",
    }
    (pack / "pack.json").write_text(
        json.dumps(
            {
                "uid": "EX-PACK",
                "name": "Ex pack",
                "ignored": [],
                "overlays": [{"path": "r.cot", "kind": "cot", "name": "r", "shapes": [shape]}],
            }
        )
    )
    got = json.loads(call(f"{base}/api/proposals?name=synth")[1])
    assert got["overlays"] == 1
    assert [p["evidence"]["label"] for p in got["proposals"] if p["kind"] == "contact"] == ["ENEMY SECTION (reported)"]


def test_the_where_route_bounds_the_time(box) -> None:
    base, _ = box
    assert call(f"{base}/api/where?name=synth&callsign=ALPHA&at=99999999999999999999")[0] == 400
    assert call(f"{base}/api/where?name=synth&callsign=ALPHA&at=-5")[0] == 400


def test_adjacent_dropouts_are_one_proposal() -> None:
    """In use, a callsign reporting every three minutes against a threshold below that made a
    dropout of every interval: 118 proposals for six hours, one per gap, each three minutes long
    (found in use, 5 September 2026). Gaps that share a report are one
    spell of thin reporting, proposed once with the count, the longest and the total."""
    import pinecone_proposals as pp

    a = walk("MilUX", T0, 5, LAT0, LON0)
    a["time"]["dropouts"] = [
        {"from": T0, "to": T0 + 180_000, "ms": 180_000},
        {"from": T0 + 180_000, "to": T0 + 360_000, "ms": 180_000},
        {"from": T0 + 360_000, "to": T0 + 600_000, "ms": 240_000},
        {"from": T0 + 3_600_000, "to": T0 + 3_800_000, "ms": 200_000},  # a separate spell, an hour later
    ]
    gaps = [p for p in pp.propose({"tracks": [a], "chat": []}, overlays=[]) if p["kind"] == "dropout"]
    assert len(gaps) == 2
    assert gaps[0]["at"] == T0 and gaps[0]["until"] == T0 + 600_000
    assert (
        gaps[0]["evidence"]["gaps"] == 3
        and gaps[0]["evidence"]["longest_ms"] == 240_000
        and gaps[0]["evidence"]["for_ms"] == 600_000
    )
    assert gaps[1]["evidence"]["gaps"] == 1 and gaps[1]["evidence"]["for_ms"] == 200_000
