"""build_bundle.py: timestamps, the detail blob, and what a bundle keeps."""

from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path

DETAIL_ATAK = (
    '<detail><contact callsign="ALPHA" endpoint="*:-1:stcp"/><__group name="Dark Green" role="Team Lead"/>'
    '<status battery="16"/><takv device="SAMSUNG SM-S911U1" platform="ATAK-CIV" os="33" version="5.8.0.1"/>'
    '<track speed="1.4" course="214.3"/><uid Droid="ALPHA"/></detail>'
)
DETAIL_MESH = (
    '<detail><takv device="" version="" platform="Meshtastic" os="Meshtastic" meshtastic_id="!dc0a12a5"/>'
    '<contact callsign="TRK1"/><status battery="60"/><track course="0.0" speed="0.0"/>'
    '<__group name="Cyan" role="TeamMember"/></detail>'
)
HEADER = "id,uid,cot_type,how,start,time,stale,servertime,lat,lon,point_hae,point_ce,point_le,detail".split(",")


def row(i: int, uid: str, t: str, lat: float, lon: float, detail: str, typ: str = "a-f-G-U-C") -> list[str]:
    return [str(i), uid, typ, "m-g", t, t, t, t, str(lat), str(lon), "95", "10", "9999999", detail]


def write_csv(path: Path, rows: list[list[str]]) -> None:
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        w.writerows(rows)


def test_timestamps_accept_what_postgres_prints_and_what_people_type(build_bundle) -> None:
    ts = build_bundle.ts
    assert ts("2026-08-31 00:00:01.26+00") == ts("2026-08-31T00:00:01.260Z")
    assert ts("2026-08-30T09:18:49.31+00:00") == 1788081529310
    assert ts("2026-09-04 07:39:43.578-04") == ts("2026-09-04T11:39:43.578Z")
    assert ts("2026-09-03T06:00Z") == ts("2026-09-03T06:00:00Z")
    assert ts("2026-09-04 11:41:01") == ts("2026-09-04T11:41:01Z")
    assert ts("") is None


def test_bundle_reads_identity_from_detail_and_keeps_every_report(build_bundle, tmp_path: Path) -> None:
    rows = [
        row(1, "ANDROID-1", "2026-09-03 08:00:00+00", 51.2130, -1.5050, DETAIL_ATAK),
        row(2, "ANDROID-1", "2026-09-03 08:00:30+00", 51.2131, -1.5051, DETAIL_ATAK),
        row(3, "ANDROID-1", "2026-09-03 08:01:00+00", 51.2132, -1.5052, DETAIL_ATAK),
        row(4, "!dc0a12a5", "2026-09-03 08:00:10+00", 51.2140, -1.5060, DETAIL_MESH),
        row(5, "!dc0a12a5", "2026-09-03 08:03:10+00", 0.0, 0.0, DETAIL_MESH),  # no fix: dropped, counted
        row(6, "!dc0a12a5", "2026-09-03 08:06:10+00", 51.2141, -1.5061, DETAIL_MESH),
        row(7, "ANDROID-1", "2026-09-03 09:00:00+00", 51.2199, -1.5099, DETAIL_ATAK),  # outside the window
    ]
    src = tmp_path / "in.csv"
    out = tmp_path / "out.json"
    write_csv(src, rows)
    r = subprocess.run(
        [
            sys.executable,
            str(build_bundle.__file__),
            str(src),
            "--out",
            str(out),
            "--start",
            "2026-09-03T08:00Z",
            "--end",
            "2026-09-03T08:30Z",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "5 points in 2 tracks (1 without fix dropped)" in r.stdout
    b = json.loads(out.read_text())
    assert b["format"] == "pinecone-bundle/0"
    # Every row read lands in exactly one count, so the four reconcile: 5 kept + 1 with no fix +
    # 0 with no readable time + 1 outside the window = 7 read. The two new keys were added because
    # the pre-UAT review of slice 1a found a row that was silently in none of them. This assertion
    # is stricter than the one it replaces, not looser.
    assert b["counts"] == {
        "rows_read": 7,
        "rows_kept": 5,
        "rows_without_fix": 1,
        "rows_without_time": 0,
        "rows_outside_window": 1,
        "tracks": 2,
    }
    c = b["counts"]
    assert c["rows_kept"] + c["rows_without_fix"] + c["rows_without_time"] + c["rows_outside_window"] == c["rows_read"]
    by = {t["uid"]: t for t in b["tracks"]}
    atak, mesh = by["ANDROID-1"], by["!dc0a12a5"]
    assert atak["callsign"] == "ALPHA" and atak["platform"] == "ATAK-CIV" and atak["device"] == "SAMSUNG SM-S911U1"
    assert atak["team"] == "Dark Green" and atak["role"] == "Team Lead"
    assert atak["median_interval_ms"] == 30_000
    assert mesh["platform"] == "Meshtastic" and mesh["meshtastic_id"] == "!dc0a12a5" and mesh["n"] == 2
    assert mesh["median_interval_ms"] == 360_000, "the dropped no-fix report does not shorten the interval"
    times = [p[0] for p in atak["points"]]
    assert times == sorted(times)
    assert atak["points"][0][4] == 1.4 and atak["points"][0][5] == 214.3 and atak["points"][0][6] == 16
    assert b["point_fields"][0] == "servertime_ms"


def test_nothing_is_interpolated(build_bundle, tmp_path: Path) -> None:
    rows = [
        row(1, "U", "2026-09-03 08:00:00+00", 51.0, -1.0, DETAIL_MESH),
        row(2, "U", "2026-09-03 09:00:00+00", 51.1, -1.1, DETAIL_MESH),
    ]
    src, out = tmp_path / "in.csv", tmp_path / "out.json"
    write_csv(src, rows)
    subprocess.run(
        [sys.executable, str(build_bundle.__file__), str(src), "--out", str(out)], check=True, capture_output=True
    )
    pts = json.loads(out.read_text())["tracks"][0]["points"]
    assert len(pts) == 2, "an hour-long gap stays two reports, never a filled-in line"
