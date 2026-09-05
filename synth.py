#!/usr/bin/env python3
# ruff: noqa: S311  seeded pseudo-randomness is the point of a deterministic fixture
"""Synthetic CoT fixture in bundle format. Deterministic. No real people.

Six callsigns around Andover: two ATAK handsets at 30 s, three Meshtastic
trackers at 2 to 4 min (one with a 22 minute dropout), one static WinTAK HQ.
Mixed rates, a deliberate gap, one Meshtastic-sourced track: what the handover
brief asks for. Writes data/synthetic.json (whitelisted in .gitignore).
"""

import itertools
import json
import math
import os
import random
import sys
from datetime import datetime, timezone

random.seed(20260904)
HERE = os.path.dirname(os.path.abspath(__file__))
T0 = int(datetime(2026, 9, 3, 9, 0, tzinfo=timezone.utc).timestamp() * 1000)
DUR = 2 * 3600 * 1000
LAT0, LON0 = 51.2135, -1.5055  # inside the Andover basemap the synthetic pack is drawn over


def offset(lat, lon, dn, de):
    return lat + dn / 111320.0, lon + de / (111320.0 * math.cos(math.radians(lat)))


def walk(waypoints, speed_mps, period_ms, jitter_m, dropout=None, start_delay=0):
    """Straight legs between waypoints at speed, sampled every period with GPS jitter."""
    pts = []
    t = T0 + start_delay
    legs = list(itertools.pairwise(waypoints))
    for a, b in legs:
        dn, de = (b[0] - a[0]) * 111320.0, (b[1] - a[1]) * 111320.0 * math.cos(math.radians(a[0]))
        dist = math.hypot(dn, de)
        leg_ms = dist / speed_mps * 1000
        course = (math.degrees(math.atan2(de, dn)) + 360) % 360
        tl = 0
        while tl < leg_ms:
            f = tl / leg_ms
            lat, lon = a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f
            lat, lon = offset(lat, lon, random.gauss(0, jitter_m), random.gauss(0, jitter_m))
            if not (dropout and dropout[0] <= t - T0 < dropout[1]):
                pts.append(
                    [
                        t,
                        round(lat, 7),
                        round(lon, 7),
                        95.0,
                        round(speed_mps + random.gauss(0, 0.2), 2),
                        round(course, 1),
                        None,
                        t + 5 * 60 * 1000,
                        t - random.randint(0, 3000),
                        "m-g",
                    ]
                )
            step = period_ms + random.randint(-period_ms // 10, period_ms // 10)
            t += step
            tl += step
            if t > T0 + DUR:
                return pts
    return pts


def square(cx, cy, size, laps=2):
    corners = [(0, 0), (size, 0), (size, size), (0, size)]
    return [offset(cx, cy, *d) for d in corners * laps + [(0, 0)]]


def loop(cx, cy, r, n=9, laps=3):
    return [
        offset(cx, cy, r * math.cos(i * 2 * math.pi / n), r * math.sin(i * 2 * math.pi / n)) for i in range(n + 1)
    ] * laps


tracks = [
    {
        "uid": "ANDROID-synth-01",
        "callsign": "SYN-ALPHA",
        "platform": "ATAK-CIV",
        "device": "SAMSUNG SM-S911U1",
        "os": "33",
        "version": "5.8.0.1-CIV",
        "team": "Dark Green",
        "role": "Team Lead",
        "type": "a-f-G-U-C",
        "points": walk(square(LAT0, LON0, 420, laps=4), 1.4, 30_000, 3),
    },
    {
        "uid": "ANDROID-synth-02",
        "callsign": "SYN-BRAVO",
        "platform": "ATAK-CIV",
        "device": "SAMSUNG SM-G525F",
        "os": "31",
        "version": "5.8.0.1-CIV",
        "team": "Dark Green",
        "role": "Team Member",
        "type": "a-f-G-U-C",
        "points": walk(loop(LAT0 + 0.002, LON0 + 0.003, 260), 1.2, 30_000, 4, start_delay=6 * 60_000),
    },
    {
        "uid": "!synth0003",
        "callsign": "SYN-TRK1",
        "platform": "Meshtastic",
        "device": "",
        "os": "Meshtastic",
        "version": "",
        "meshtastic_id": "!synth0003",
        "team": "Cyan",
        "role": "Team Member",
        "type": "a-f-G-U-C",
        "points": walk(loop(LAT0 - 0.0015, LON0 - 0.002, 300, 7), 1.3, 150_000, 6),
    },
    {
        "uid": "!synth0004",
        "callsign": "SYN-TRK2",
        "platform": "Meshtastic",
        "device": "",
        "os": "Meshtastic",
        "version": "",
        "meshtastic_id": "!synth0004",
        "team": "Cyan",
        "role": "Team Member",
        "type": "a-f-G-U-C",
        "points": walk(
            square(LAT0 - 0.003, LON0 + 0.001, 350, laps=4), 1.1, 200_000, 8, dropout=(40 * 60_000, 62 * 60_000)
        ),
    },
    {
        "uid": "!synth0005",
        "callsign": "SYN-TRK3",
        "platform": "Meshtastic",
        "device": "",
        "os": "Meshtastic",
        "version": "",
        "meshtastic_id": "!synth0005",
        "team": "Cyan",
        "role": "Team Member",
        "type": "a-f-G-U-C",
        "points": walk(loop(LAT0 + 0.001, LON0 - 0.004, 200, 6, laps=5), 1.5, 240_000, 10, start_delay=15 * 60_000),
    },
    {
        "uid": "WINTAK-synth-06",
        "callsign": "SYN-HQ",
        "platform": "WinTAK-CIV",
        "device": "HQ laptop",
        "os": "Windows",
        "version": "5.4",
        "team": "White",
        "role": "HQ",
        "type": "a-f-G-U-C",
        "points": [
            [
                T0 + i * 60_000,
                LAT0 + 0.0005,
                LON0 + 0.0005,
                96.0,
                0.0,
                0.0,
                None,
                T0 + i * 60_000 + 300_000,
                T0 + i * 60_000,
                "h-e",
            ]
            for i in range(0, DUR // 60_000)
        ],
    },
]
out = []
for tr in tracks:
    pts = tr.pop("points")
    gaps = [b[0] - a[0] for a, b in itertools.pairwise(pts)]
    gaps.sort()
    tr.update(
        n=len(pts),
        first=pts[0][0],
        last=pts[-1][0],
        median_interval_ms=gaps[len(gaps) // 2] if gaps else None,
        points=pts,
    )
    tr.setdefault("meshtastic_id", "")
    out.append(tr)
bundle = {
    "format": "pinecone-bundle/0",
    "source": {
        "name": "synthetic fixture (synth.py, seeded)",
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    },
    "window": {"start": T0, "end": T0 + DUR},
    "counts": {
        "rows_read": sum(t["n"] for t in out),
        "rows_kept": sum(t["n"] for t in out),
        "rows_without_fix": 0,
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
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "data", "synthetic.json")
os.makedirs(os.path.dirname(OUT), exist_ok=True)
with open(OUT, "w") as f:
    json.dump(bundle, f, separators=(",", ":"))
print(OUT + ":", [(t["callsign"], t["n"]) for t in out])
