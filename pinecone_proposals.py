#!/usr/bin/env python3
"""Proposals, never narration: the moments the record can point at, with their evidence and
without a reason.

D7 says the differentiator is the layer over the recording. The doctrine says the constraint just
as firmly: detect co-location, comms gaps, boundary crossings, contact and long silences; propose;
let a human accept or reject; never answer why. Everything here is arithmetic over the bundle the
player already reads and the overlays it already draws. There is no model, and a model that comes
later is held to the same shape: a kind, a time, the callsigns, the evidence, and no field for a
reason, a fault or a rating (D8: never a grading instrument).

Standard library only (ADR-002). Nothing here reads a file except the dismissals.
"""

from __future__ import annotations

import bisect
import hashlib
import itertools
import json
import math
import os
import re
from typing import Any

CO_LOCATION_M = 50.0
CO_LOCATION_MS = 3 * 60_000
SILENCE_MS = 10 * 60_000
PROPOSALS_CAP = 200
DISMISSED_CAP = 2000  # identities kept on the box; the oldest go first
NEAR_DEG = 0.001  # about 110 m of latitude, a coarse box that CO_LOCATION_M sits well inside
DEFAULT_STALE_MS = 300_000
# Words in a message worth a look. A match proposes the message; it asserts nothing.
WATCH_WORDS = ("casevac", "medevac", "casualty", "9 liner", "nine liner", "troops in contact", "contact")
# Whole words: "contactless" and "CONTACTS list" were proposing (review note N4).
_WORD_RE = re.compile(
    r"(?<![a-z0-9])(?:" + "|".join(re.escape(w) for w in sorted(WATCH_WORDS, key=len, reverse=True)) + r")(?![a-z0-9])",
    re.I,
)

Point = list[Any]  # [servertime, lat, lon, hae, speed, course, battery, stale, device time, how]


def _threshold(track: dict[str, Any]) -> int:
    t = track.get("time") or {}
    if t.get("dropout_threshold_ms"):
        return int(t["dropout_threshold_ms"])
    mi = track.get("median_interval_ms")
    return int(min(3_600_000, max(90_000, 4 * mi))) if mi else DEFAULT_STALE_MS


def metres(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance. Reports are close enough together that the sphere is exact enough."""
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _last_at(points: list[Point], times: list[int], at_ms: int) -> Point | None:
    i = bisect.bisect_right(times, at_ms) - 1
    return points[i] if i >= 0 else None


def where_was(bundle: dict[str, Any], callsign: str, at_ms: int) -> dict[str, Any]:
    """The last report at or before a moment, with how old it was then, and whether that is stale
    by the reporting node's own threshold. Unknown is said to be unknown.

    One handset is often two nodes with one callsign (a phone that is both an ATAK client and a mesh
    node), so every track carrying the name is asked and the newest report answers; the answer
    says which node it came from and how many carry the name (review finding G3)."""
    wanted = (callsign or "").strip().lower()
    tracks = [t for t in bundle.get("tracks", []) if str(t.get("callsign", "")).lower() == wanted]
    if not tracks:
        return {
            "known": False,
            "callsign": callsign,
            "message": f"No callsign {callsign} in this window. Check the spelling against the list, or widen the window.",
        }
    best: tuple[int, dict[str, Any], Point] | None = None
    for t in tracks:
        pts = sorted(t["points"], key=lambda p: p[0])
        p = _last_at(pts, [int(p[0]) for p in pts], at_ms)
        if p is not None and (best is None or int(p[0]) > best[0]):
            best = (int(p[0]), t, p)
    name = tracks[0]["callsign"]
    if best is None:
        return {
            "known": False,
            "callsign": name,
            "nodes": len(tracks),
            "message": f"{name} had not reported yet at that moment. Move the clock later and ask again.",
        }
    when, track, p = best
    age = int(at_ms - when)
    return {
        "known": True,
        "callsign": name,
        "uid": track.get("uid"),
        "nodes": len(tracks),
        "at": when,
        "age_ms": age,
        "stale": age > _threshold(track),
        "threshold_ms": _threshold(track),
        "lat": p[1],
        "lon": p[2],
        "latency_ms": (int(when - p[8]) if p[8] is not None else None),
    }


def _ident(kind: str, at: int, callsigns: list[str], extra: str = "") -> str:
    raw = f"{kind}|{at}|{'|'.join(sorted(callsigns))}|{extra}"
    return hashlib.sha256(raw.encode()).hexdigest()[:12]  # an identity for dismissals, not a secret


def _proposal(
    kind: str, at: int, callsigns: list[str], evidence: dict[str, Any], until: int | None = None, extra: str = ""
) -> dict[str, Any]:
    return {
        "id": _ident(kind, at, callsigns, extra),
        "kind": kind,
        "at": int(at),
        "until": int(until) if until is not None else None,
        "callsigns": sorted(callsigns),
        "evidence": evidence,
    }


def _co_location(tracks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    prepared = []
    for t in tracks:
        pts = sorted(t["points"], key=lambda p: p[0])
        prepared.append((t, pts, [int(p[0]) for p in pts], _threshold(t)))

    def close_run(a: dict[str, Any], b: dict[str, Any], start: int, last: int, closest: float) -> None:
        if last - start >= CO_LOCATION_MS:
            out.append(
                _proposal(
                    "co-location",
                    start,
                    [a["callsign"], b["callsign"]],
                    {"closest_m": round(closest, 1), "for_ms": last - start},
                    until=last,
                )
            )

    for i in range(len(prepared)):
        for j in range(i + 1, len(prepared)):
            a, apts, _, _ = prepared[i]
            b, bpts, btimes, bthr = prepared[j]
            if str(a.get("callsign", "")).lower() == str(b.get("callsign", "")).lower():
                continue  # one handset as two nodes is not two people meeting (review note N5)
            start: int | None = None
            last = 0
            closest = 0.0
            for p in apts:
                q = _last_at(bpts, btimes, int(p[0]))
                # a rough box before the trigonometry: most pairs are nowhere near each other
                if q is None or abs(p[1] - q[1]) > NEAR_DEG or abs(p[2] - q[2]) > 2 * NEAR_DEG:
                    d = math.inf
                else:
                    d = metres(p[1], p[2], q[1], q[2])
                near = q is not None and (p[0] - q[0]) <= bthr and d <= CO_LOCATION_M
                if near:
                    if start is None:
                        start, closest = int(p[0]), d
                    last = int(p[0])
                    closest = min(closest, d)
                elif start is not None:
                    close_run(a, b, start, last, closest)
                    start = None
            if start is not None:
                close_run(a, b, start, last, closest)
    return out


def _dropouts(tracks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One proposal per spell of thin reporting, not one per gap. Gaps that share a report (the end
    of one is the start of the next) are one spell: in use, a callsign reporting every three
    minutes against a lower threshold made 118 proposals in six hours, one per interval."""
    out = []
    for t in tracks:
        gaps = sorted((d for d in (t.get("time") or {}).get("dropouts") or []), key=lambda d: d["from"])
        spells: list[list[dict[str, Any]]] = []
        for d in gaps:
            if spells and spells[-1][-1]["to"] == d["from"]:
                spells[-1].append(d)
            else:
                spells.append([d])
        for spell in spells:
            first, last = spell[0], spell[-1]
            out.append(
                _proposal(
                    "dropout",
                    first["from"],
                    [t["callsign"]],
                    {
                        "gaps": len(spell),
                        "for_ms": sum(int(d["ms"]) for d in spell),
                        "longest_ms": max(int(d["ms"]) for d in spell),
                        "threshold_ms": _threshold(t),
                    },
                    until=last["to"],
                )
            )
    return out


def _silences(tracks: list[dict[str, Any]], chat: list[dict[str, Any]]) -> list[dict[str, Any]]:
    stamps = sorted(
        {int(p[0]) for t in tracks for p in t["points"]} | {int(m["servertime"]) for m in chat if m.get("servertime")}
    )
    out = []
    for x, y in itertools.pairwise(stamps):
        if y - x > SILENCE_MS:
            out.append(_proposal("silence", x, [], {"for_ms": y - x, "no_report_and_no_message": True}, until=y))
    return out


def _inside(lat: float, lon: float, ring: list[list[float]]) -> bool:
    """Ray casting over (lat, lon). A point exactly on an edge is a coin toss, as it is for every
    algorithm; the tests keep off the edges and so should anyone reading a proposal at one."""
    inside = False
    n = len(ring)
    for i in range(n):
        y1, x1 = ring[i]
        y2, x2 = ring[(i + 1) % n]
        if (y1 > lat) != (y2 > lat):
            x = x1 + (lat - y1) * (x2 - x1) / (y2 - y1)
            if lon < x:
                inside = not inside
    return inside


def _applies(shape: dict[str, Any], at: int) -> bool:
    if shape.get("undated") or shape.get("window_unreadable"):
        return True
    b, e = shape.get("begin_ms"), shape.get("end_ms")
    return (b is None or at >= b) and (e is None or at < e)


def _boundaries(tracks: list[dict[str, Any]], overlays: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    polys = [
        s
        for o in overlays
        for s in (o.get("shapes") or [])
        if s.get("kind") == "polygon" and not s.get("reported") and len(s.get("coordinates") or []) >= 3
    ]
    for t in tracks:
        pts = sorted(t["points"], key=lambda p: p[0])
        for shape in polys:
            name = shape.get("label") or shape.get("name") or "an overlay"
            was: bool | None = None
            for p in pts:
                if not _applies(shape, int(p[0])):
                    continue
                now = _inside(p[1], p[2], shape["coordinates"])
                if was is not None and now != was:
                    out.append(
                        _proposal(
                            "boundary",
                            int(p[0]),
                            [t["callsign"]],
                            {"overlay": name, "direction": "in" if now else "out"},
                            extra=name,
                        )
                    )
                was = now
    return out


def _contacts(overlays: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for o in overlays:
        for s in o.get("shapes") or []:
            if s.get("reported") and s.get("begin_ms"):
                label = s.get("label") or s.get("name") or "reported contact"
                out.append(
                    _proposal(
                        "contact",
                        int(s["begin_ms"]),
                        [],
                        {"reported": True, "label": label, "ce": s.get("ce")},
                        extra=label,
                    )
                )
    return out


def _messages(chat: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for m in chat:
        text = m.get("text") or ""
        hit = _WORD_RE.search(text)
        if not hit or not m.get("servertime"):
            continue
        who = [m["sender"]] if m.get("sender") else []
        out.append(
            _proposal(
                "message",
                int(m["servertime"]),
                who,
                {"word": hit.group(0), "text": text, "room": m.get("room") or ""},
                extra=str(m.get("uid") or text),
            )
        )
    return out


def propose(
    bundle: dict[str, Any], overlays: list[dict[str, Any]], dismissed: frozenset[str] | set[str] | None = None
) -> list[dict[str, Any]]:
    """Every moment the record can point at in this window, in time order, capped, minus the ones
    this box has dismissed. Nothing here says why."""
    tracks = [t for t in bundle.get("tracks", []) if t.get("points")]
    chat = bundle.get("chat") or []
    found = (
        _co_location(tracks)
        + _dropouts(tracks)
        + _silences(tracks, chat)
        + _boundaries(tracks, overlays)
        + _contacts(overlays)
        + _messages(chat)
    )
    w = bundle.get("window") or {}
    if isinstance(w.get("start"), int) and isinstance(w.get("end"), int):
        # A pack's reported contacts carry their own dates; one from three weeks ago headed the
        # list of a one-hour window (review finding G2). Nothing outside the window is proposed.
        found = [p for p in found if w["start"] <= p["at"] < w["end"]]
    gone = set(dismissed or ())
    found = [p for p in found if p["id"] not in gone]
    found.sort(key=lambda p: (p["at"], p["kind"], p["id"]))
    return found[:PROPOSALS_CAP]


def _read_dismissed(path: str) -> list[str]:
    """The file as a list, oldest first; a damaged file reads as empty rather than failing every
    request that comes after it."""
    try:
        with open(path, encoding="utf-8") as fh:
            got = json.load(fh)
    except (OSError, ValueError):
        return []
    return [x for x in got if isinstance(x, str)] if isinstance(got, list) else []


def load_dismissed(path: str) -> frozenset[str]:
    return frozenset(_read_dismissed(path))


def dismiss(path: str, proposal_id: str) -> None:
    """Kept in order of dismissal and bounded, so a box used for years does not carry a file that
    grows without limit; when the cap is reached the oldest dismissal is forgotten first."""
    ids = [x for x in _read_dismissed(path) if x != proposal_id]
    ids.append(str(proposal_id))
    ids = ids[-DISMISSED_CAP:]
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(ids, fh)
    os.replace(tmp, path)
