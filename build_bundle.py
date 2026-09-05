#!/usr/bin/env python3
"""Turn a pulled cot_router CSV into a Pinecone bundle (the seam, D11).

usage: build_bundle.py data/server.csv --out data/2026-09-03.json \
           [--start 2026-09-03T00:00Z] [--end 2026-09-04T00:00Z]

Nothing is interpolated or smoothed (D9). Every report in the window is kept,
in servertime order, with device time and stale alongside (D10). Reports with
no fix (lat and lon both 0) are dropped and counted.
"""

import argparse
import csv
import html
import itertools
import json
import re
import statistics
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

csv.field_size_limit(1 << 26)

RE = {
    "callsign": re.compile(r'<contact[^>]*\scallsign="([^"]*)"'),
    "chat_sender": re.compile(r'<__chat[^>]*\ssenderCallsign="([^"]*)"'),
    "chat_room": re.compile(r'<__chat[^>]*\schatroom="([^"]*)"'),
    "remarks": re.compile(r"<remarks[^>]*>(.*?)</remarks>", re.S),
    "takv": re.compile(r"<takv([^>]*)/?>"),
    "group": re.compile(r'<__group[^>]*\sname="([^"]*)"[^>]*\srole="([^"]*)"'),
    "speed": re.compile(r'<track[^>]*\sspeed="([^"]*)"'),
    "course": re.compile(r'<track[^>]*\scourse="([^"]*)"'),
    "battery": re.compile(r'<status[^>]*\sbattery="([^"]*)"'),
    "attr": re.compile(r'(\w+)="([^"]*)"'),
}

TS_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?(?:\.(\d{1,9}))?\s*(Z|[+-]\d{2}(?::?\d{2})?)?$"
)


def ts(s):
    """Postgres or ISO timestamp to epoch ms. Hand-parsed: Python before 3.11 rejects
    fractions that are not 3 or 6 digits, and Postgres prints '.31+00'."""
    if not s:
        return None
    m = TS_RE.match(s.strip())
    if not m:
        raise ValueError(f"unrecognised timestamp: {s!r}")
    y, mo, d, h, mi, sec, frac, tz = m.groups()
    us = int((frac or "0").ljust(6, "0")[:6])
    off = timezone.utc
    if tz and tz != "Z":
        sign = 1 if tz[0] == "+" else -1
        hh, mm = tz[1:3], (tz[3:].replace(":", "") or "00")
        off = timezone(sign * timedelta(hours=int(hh), minutes=int(mm)))
    dt = datetime(int(y), int(mo), int(d), int(h), int(mi), int(sec or 0), us, tzinfo=off)
    return int(dt.timestamp() * 1000)


def num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


DROPOUT_MULTIPLE = 4  # a gap longer than this many median intervals is a dropout; the player's default too
# The threshold is clamped, as the player always clamped it. An ATAK handset reports in bursts
# with a median interval under a second, and four times that would call every breath between
# bursts a dropout; a tracker on a three-minute cadence would otherwise wait twelve minutes to be
# called missing, which is about right, but nothing should wait more than an hour.
DROPOUT_FLOOR_MS = 90_000
SAME_REPORT_MS = 1_000  # events closer than this are one report, for the cadence
DROPOUT_CEILING_MS = 3_600_000


def honest_time(pts: list[list[Any]], median_interval: float | None) -> dict[str, Any]:
    """Latency, clock disagreement and dropouts, measured from what the record already holds.

    Latency is the server's receipt time minus the device's own time (D10 keeps both). A report
    whose device time is after its server time came from a clock running ahead of the server's;
    it is counted and shown, and kept out of the latency figures, because folding a negative in
    would make a bad clock look like a fast link. Unknown stays unknown: a report with no device
    time contributes nothing, and a callsign with none has no latency figure rather than zero.

    A dropout is a gap longer than DROPOUT_MULTIPLE median intervals, the same rule the player
    uses, and the threshold travels in the bundle so the two cannot disagree. Nothing here says why
    a dropout happened; that is a person's question.
    """
    latencies: list[int] = []
    ahead: list[int] = []
    for p in pts:
        server_ms, device_ms = p[0], p[8]
        if device_ms is None:
            continue
        d = int(server_ms - device_ms)
        if d < 0:
            ahead.append(-d)
        else:
            latencies.append(d)
    latencies.sort()

    def pct(values: list[int], q: float) -> int | None:
        if not values:
            return None
        k = min(len(values) - 1, max(0, round(q * (len(values) - 1))))
        return values[k]

    threshold = (
        int(min(DROPOUT_CEILING_MS, max(DROPOUT_FLOOR_MS, DROPOUT_MULTIPLE * median_interval)))
        if median_interval
        else None
    )
    dropouts: list[dict[str, int]] = []
    if threshold:
        for a_, b in itertools.pairwise(pts):
            gap = b[0] - a_[0]
            if gap > threshold:
                dropouts.append({"from": int(a_[0]), "to": int(b[0]), "ms": int(gap)})
    return {
        "latency_known": len(latencies),
        "latency_median_ms": int(statistics.median(latencies)) if latencies else None,
        "latency_p95_ms": pct(latencies, 0.95),
        "latency_max_ms": latencies[-1] if latencies else None,
        "clock_ahead_count": len(ahead),
        "clock_ahead_max_ms": max(ahead) if ahead else None,
        "dropout_threshold_ms": threshold,
        "dropouts": dropouts,
        "missing_ms": sum(d["ms"] for d in dropouts),
    }


def bundle_from_rows(rows, start_ms=None, end_ms=None, source="cot_router", origin=None):
    """Turn reports into a bundle. The one place a bundle is made, whether the rows came from a CSV
    pulled off a server or from Pinecone's own archive.

    Nothing is interpolated, smoothed or thinned (D9). Reports with no fix are dropped and counted,
    and so are reports with no readable server time and reports outside the window: every row read
    is accounted for in one of the counts, so the four of them reconcile.
    """
    tracks: dict[str, dict[str, Any]] = {}
    chat: list[dict[str, Any]] = []
    nofix = total = kept = notime = outside = 0
    for row in rows:
        total += 1
        t = ts(row["servertime"])
        if t is None:
            notime += 1
            continue
        if (start_ms and t < start_ms) or (end_ms and t >= end_ms):
            outside += 1
            continue
        if str(row.get("cot_type") or "").startswith("b-t-f"):
            # A message, not a position. Read for what it says, stored as it arrived; a malformed
            # one is listed with what it has rather than dropped, because it is what arrived.
            d = row.get("detail") or ""
            sender = RE["chat_sender"].search(d)
            room = RE["chat_room"].search(d)
            text = RE["remarks"].search(d)
            chat.append(
                {
                    "uid": row.get("uid"),
                    "sender": html.unescape(sender.group(1)) if sender else "",
                    "room": html.unescape(room.group(1)) if room else "",
                    "text": html.unescape(text.group(1).strip()) if text else "",
                    "servertime": t,
                    "time": ts(row.get("time")),
                    "lat": num(row.get("lat")),
                    "lon": num(row.get("lon")),
                }
            )
            kept += 1
            continue
        lat, lon = num(row["lat"]), num(row["lon"])
        if lat is None or lon is None or (abs(lat) < 1e-6 and abs(lon) < 1e-6):
            nofix += 1
            continue
        d = row["detail"] or ""
        tr = tracks.setdefault(
            row["uid"],
            {
                "uid": row["uid"],
                "points": [],
                "callsigns": Counter(),
                "types": Counter(),
                "takv": {},
                "team": None,
                "role": None,
            },
        )
        m = RE["callsign"].search(d)
        if m and m.group(1):
            tr["callsigns"][m.group(1)] += 1
        m = RE["takv"].search(d)
        if m:
            tr["takv"] = dict(RE["attr"].findall(m.group(1)))
        m = RE["group"].search(d)
        if m:
            tr["team"], tr["role"] = m.group(1), m.group(2)
        tr["types"][row["cot_type"]] += 1
        sp = RE["speed"].search(d)
        co = RE["course"].search(d)
        ba = RE["battery"].search(d)
        tr["points"].append(
            [
                t,
                round(lat, 7),
                round(lon, 7),
                num(row.get("point_hae")),
                num(sp.group(1)) if sp else None,
                num(co.group(1)) if co else None,
                num(ba.group(1)) if ba else None,
                ts(row.get("stale")),
                ts(row.get("time")),
                row.get("how") or None,
            ]
        )
        kept += 1

    out = []
    for uid, tr in tracks.items():
        pts = sorted(tr["points"], key=lambda p: p[0])
        # The cadence is the gap between reports. A handset that sends two events in the same
        # instant (one handset does, every three minutes) has a cadence of three minutes, not of the
        # milliseconds between the pair; counted raw, the median was 586 ms, the threshold fell to
        # its floor and every interval read as a dropout (found in use, 5 September 2026).
        gaps = [b[0] - a_[0] for a_, b in itertools.pairwise(pts) if b[0] - a_[0] >= SAME_REPORT_MS]
        med = statistics.median(gaps) if gaps else None
        time_facts = honest_time(pts, med)
        tk = tr["takv"]
        out.append(
            {
                "uid": uid,
                "callsign": tr["callsigns"].most_common(1)[0][0] if tr["callsigns"] else uid,
                "platform": tk.get("platform") or "",
                "device": tk.get("device") or "",
                "os": tk.get("os") or "",
                "version": tk.get("version") or "",
                "meshtastic_id": tk.get("meshtastic_id") or "",
                "team": tr["team"],
                "role": tr["role"],
                "type": tr["types"].most_common(1)[0][0],
                "n": len(pts),
                "first": pts[0][0],
                "last": pts[-1][0],
                "median_interval_ms": int(med) if med else None,
                "time": time_facts,
                "points": pts,
            }
        )
    out.sort(key=lambda t: -t["n"])
    allt = [p[0] for t in out for p in t["points"]]
    src: dict[str, Any] = {"name": source, "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if origin:
        src.update(origin)
    return {
        "format": "pinecone-bundle/0",
        "source": src,
        "window": {
            "start": start_ms if start_ms is not None else (min(allt) if allt else 0),
            "end": end_ms if end_ms is not None else ((max(allt) + 1) if allt else 0),
        },
        "time": {
            "note": (
                "What the record holds is what the server received, when it received it. It does not hold "
                "what any handset displayed, so nothing here says what anyone saw; only what arrived, and "
                "how late."
            ),
            "dropout_multiple": DROPOUT_MULTIPLE,
            "dropout_floor_ms": DROPOUT_FLOOR_MS,
            "dropout_ceiling_ms": DROPOUT_CEILING_MS,
        },
        "chat": sorted(chat, key=lambda m: (m["servertime"], str(m["uid"]))),
        "counts": {
            "rows_read": total,
            "rows_kept": kept,
            "rows_without_fix": nofix,
            "rows_without_time": notime,
            "rows_outside_window": outside,
            "tracks": len(out),
        },
        "point_fields": [
            "servertime_ms",
            "lat",
            "lon",
            "hae",
            "speed",
            "course",
            "battery",
            "stale_ms",
            "device_time_ms",
            "how",
        ],
        "tracks": out,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--out", required=True)
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--source", default="cot_router")
    a = ap.parse_args()
    w0 = ts(a.start) if a.start else None
    w1 = ts(a.end) if a.end else None
    with open(a.csv, newline="") as f:
        bundle = bundle_from_rows(csv.DictReader(f), w0, w1, a.source, origin={"csv": a.csv})
    with open(a.out, "w") as f:
        json.dump(bundle, f, separators=(",", ":"))
    c = bundle["counts"]
    print(f"{a.out}: {c['rows_kept']} points in {c['tracks']} tracks ({c['rows_without_fix']} without fix dropped)")
    for t in bundle["tracks"]:
        mi = t["median_interval_ms"]
        print(
            f"  {t['callsign']:<18} {t['platform']:<10} n={t['n']:<5} median={mi/1000 if mi else 0:>7.1f}s "
            f"{datetime.fromtimestamp(t['first']/1000, timezone.utc):%d %H:%M}Z..{datetime.fromtimestamp(t['last']/1000, timezone.utc):%d %H:%M}Z"
        )


if __name__ == "__main__":
    main()
