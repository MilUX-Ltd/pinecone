#!/usr/bin/env python3
"""The structured record: one debrief's record, in the unit's own shape, kept on the box and taken
away as a file.

Phases 5 to 7 of an after-action review happen without the tool; its only contribution is to emit
a structured record, and the twenty-five-year-old complaint about take-home packages is that they
are generic and late. This one is specific, short, and leaves the room with the room.

One atom renders as either shape. An item carries the four ODCR fields (observation, discussion,
conclusion, recommendation), a kind (sustain or improve, or not yet sorted) and an owner that is a
duty position. The ODCR shape renders all four; the sustain-and-improve shape groups by kind and
renders observation, discussion and recommendation. The tool writes nothing into a record but what
the room typed and the moments the room kept (D8: no per-person section, no judgement).

Standard library only (ADR-002). Pure functions over dicts; the server owns the file and the lock.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

SHAPES = ("odcr", "sustain-improve")
KINDS = ("sustain", "improve")
RECORDS_CAP = 50
ITEMS_CAP = 12
DOCTRINE = {"sustain": 3, "improve": 3}  # the number the research recommends, printed beside the budget
TITLE_LIMIT = 200
OBJECTIVES_LIMIT = 4000
FIELD_LIMIT = 4000
OWNER_LIMIT = 80
FIELDS = ("observation", "discussion", "conclusion", "recommendation")
# a moment's time, and an item's: a millisecond timestamp between 2000 and 2100
AT_MIN, AT_MAX = 946_684_800_000, 4_133_980_800_000
ID_RE = re.compile(r"^[0-9a-f]{12}$")


def chosen_shape(env: Mapping[str, str]) -> str:
    """The box's shape, from the environment file. Anything but the two shapes reads as the default."""
    want = env.get("PINECONE_RECORD", "").strip().lower()
    return want if want in SHAPES else SHAPES[0]


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def clean_title(raw: str) -> str:
    """One line of text, judged only by its length. The page renders it as text."""
    title = " ".join((raw or "").split())
    if not title:
        raise ValueError("a record needs a title")
    if len(title) > TITLE_LIMIT:
        raise ValueError(f"a title is at most {TITLE_LIMIT} characters")
    return title


def clean_text(raw: str, what: str, limit: int) -> str:
    text = (raw or "").replace("\r\n", "\n").strip()
    if len(text) > limit:
        raise ValueError(f"{what} is at most {limit} characters")
    return text


def clean_window(raw_start: str, raw_end: str) -> dict[str, int]:
    try:
        start, end = int(str(raw_start).replace("_", "x")), int(str(raw_end).replace("_", "x"))
    except ValueError as e:
        raise ValueError("a record's window is a start and an end, each in milliseconds") from e
    if not (AT_MIN <= start <= AT_MAX and AT_MIN <= end <= AT_MAX):
        raise ValueError("a record's window is between 2000 and 2100")
    if start >= end:
        raise ValueError("the start of a window must come before its end")
    return {"start": start, "end": end}


def new_record(title: str, raw_start: str, raw_end: str, objectives: str = "") -> dict[str, Any]:
    return {
        "id": new_id(),
        "title": clean_title(title),
        "window": clean_window(raw_start, raw_end),
        "objectives": clean_text(objectives, "What was supposed to happen", OBJECTIVES_LIMIT),
        "created": _now(),
        "items": [],
    }


def clean_item(form: Mapping[str, str], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    """An item from a form, or an existing item with the form's fields changed. Every field is the
    room's; the tool judges only lengths and the two names a kind may have."""
    item: dict[str, Any] = (
        dict(existing)
        if existing
        else {
            "id": new_id(),
            "observation": "",
            "discussion": "",
            "conclusion": "",
            "recommendation": "",
            "kind": "",
            "owner": "",
            "at": None,
            "created": _now(),
        }
    )
    for f in FIELDS:
        if f in form:
            item[f] = clean_text(form[f], f"the {f}", FIELD_LIMIT)
    if "kind" in form:
        kind = (form["kind"] or "").strip().lower()
        if kind not in ("", *KINDS):
            raise ValueError("a kind is sustain or improve, or left unsorted")
        item["kind"] = kind
    if "owner" in form:
        owner = " ".join((form["owner"] or "").split())
        if len(owner) > OWNER_LIMIT:
            raise ValueError(f"an owner is a duty position of at most {OWNER_LIMIT} characters")
        item["owner"] = owner
    if "at" in form:
        raw = (form["at"] or "").strip()
        if raw == "":
            item["at"] = None
        else:
            try:
                at = int(raw.replace("_", "x"))
            except ValueError as e:
                raise ValueError("an item's time is a millisecond timestamp") from e
            if not (AT_MIN <= at <= AT_MAX):
                raise ValueError("an item's time is a millisecond timestamp between 2000 and 2100")
            item["at"] = at
    if not item["observation"]:
        raise ValueError("an item needs an observation")
    return item


def item_from_moment(moment: Mapping[str, Any]) -> dict[str, Any]:
    """The one prefill the tool makes: the moment's own name and time, and nothing else."""
    return clean_item({"observation": str(moment.get("name") or ""), "at": str(moment.get("at") or "")})


# ---- the store ----------------------------------------------------------------------------------


def _clean_stored_item(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not raw["id"]:
        return None
    # An observation a hand edit blanked does not lose the discussion the room typed: the item is
    # kept with what it has and the export says "(no observation)", as the moments and the chat
    # degrade (review of slice 7, note 13). A new item still needs one at the API.
    obs = raw.get("observation") if isinstance(raw.get("observation"), str) else ""
    at = raw.get("at")
    kind = raw.get("kind")
    return {
        "id": raw["id"],
        "observation": obs,
        "discussion": raw.get("discussion") if isinstance(raw.get("discussion"), str) else "",
        "conclusion": raw.get("conclusion") if isinstance(raw.get("conclusion"), str) else "",
        "recommendation": raw.get("recommendation") if isinstance(raw.get("recommendation"), str) else "",
        "kind": kind if kind in KINDS else "",
        "owner": raw.get("owner") if isinstance(raw.get("owner"), str) else "",
        "at": at if isinstance(at, int) and not isinstance(at, bool) and AT_MIN <= at <= AT_MAX else None,
        "created": raw.get("created") if isinstance(raw.get("created"), str) else "",
    }


def load_records(path: str) -> list[dict[str, Any]]:
    """The store, degraded to what can be read, entry by entry and item by item, so a hand edit that
    damages one record does not hide the rest."""
    try:
        with open(path, encoding="utf-8") as fh:
            got = json.load(fh)
    except (OSError, ValueError):
        return []
    if not isinstance(got, list):
        return []
    kept: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in got:
        if not isinstance(r, dict) or not isinstance(r.get("id"), str) or not r["id"] or r["id"] in seen:
            continue
        if not isinstance(r.get("title"), str) or not r["title"].strip():
            continue
        w = r.get("window")
        if (
            not isinstance(w, dict)
            or not all(isinstance(w.get(k), int) for k in ("start", "end"))
            or w["start"] >= w["end"]
        ):
            continue
        if not isinstance(r.get("items"), list):
            continue
        items = [i for i in (_clean_stored_item(x) for x in r["items"]) if i is not None]
        kept.append(
            {
                "id": r["id"],
                "title": r["title"],
                "window": {"start": w["start"], "end": w["end"]},
                "objectives": r.get("objectives") if isinstance(r.get("objectives"), str) else "",
                "created": r.get("created") if isinstance(r.get("created"), str) else "",
                "items": items[:ITEMS_CAP],
            }
        )
        seen.add(r["id"])
    return kept[:RECORDS_CAP]


def save_records(path: str, records: list[dict[str, Any]]) -> None:
    """Whole, to a temporary name, renamed into place: a crash mid-write leaves the previous file."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(records, fh, separators=(",", ":"))
    os.replace(tmp, path)


def summary(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": record["id"],
        "title": record["title"],
        "window": record["window"],
        "objectives": record.get("objectives", ""),
        "created": record.get("created", ""),
        "items": len(record["items"]),
    }


# ---- the export ---------------------------------------------------------------------------------

# What CommonMark reads as structure at the start of a line: a heading, a list marker or a number
# followed by a space, a quote, a fence, a table row, and a line made only of the characters that
# draw a rule or underline a setext heading. "#hashtag" and "-5 degrees" are text and stay text.
_LINE_LEAD = re.compile(
    r"^(\s*)(#{1,6}(?=\s|$)|[+*\-](?=\s|$)|\d{1,9}[.)](?=\s|$)|`{3,}|~{3,}|\||>|[-=*_](?=[-=*_ \t]*$))"
)


def md_text(raw: str) -> str:
    """Operator text on its way into a Markdown file, escaped so it reads as text wherever the file
    is opened: a `<` cannot open a tag, a `[` cannot start a link or an image, and a line cannot
    become a heading, a list, a quote, a rule, a table or a fence it was not typed as. Backslash
    escapes are CommonMark and read as themselves in a plain text editor; a `>` mid-line is left
    alone because it means nothing there and "5 > 3" should read as typed (review of slice 7)."""
    out = []
    for line in (raw or "").replace("\r\n", "\n").split("\n"):
        line = line.replace("\\", "\\\\").replace("<", "\\<").replace("[", "\\[")
        line = _LINE_LEAD.sub(lambda m: m.group(1) + "\\" + m.group(2), line, count=1)
        out.append(line)
    return "\n".join(out)


def _utc(ms: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ms / 1000)) + "Z"


def _clock(ms: int) -> str:
    return time.strftime("%H:%M:%S", time.gmtime(ms / 1000)) + "Z"


def render_markdown(
    record: Mapping[str, Any],
    moments: Sequence[Mapping[str, Any]],
    shape: str,
    box: str,
    version: str,
    now_ms: int,
) -> str:
    """The record as a file. The moments passed in are the promoted ones inside the record's window;
    they are read live by the caller so the file and the page never disagree."""
    w = record["window"]
    lines = [f"# {md_text(record['title'])}", ""]
    lines.append(
        f"Training record made by Pinecone {md_text(version)} on {md_text(box)} at {_utc(now_ms)}, for the window "
        f"{_utc(w['start'])} to {_utc(w['end'])}. Contains callsigns from the exercise; it is training material "
        "and is handled as such. Nothing in it was written by the tool: the observations, discussion, "
        "conclusions and recommendations are the room's, and the moments are the ones the room kept."
    )
    lines += ["", "## What was supposed to happen", ""]
    lines.append(md_text(record.get("objectives") or "") or "(not recorded)")
    lines += ["", "## Moments", ""]
    kept = [m for m in moments if m.get("promoted")]
    if kept:
        for m in sorted(kept, key=lambda m: int(m.get("at", 0))):
            lines.append(f"- {_clock(int(m['at']))} {md_text(str(m.get('name') or ''))}")
    else:
        lines.append("- none promoted")
    items = list(record.get("items") or [])

    def item_block(n: int, it: Mapping[str, Any], fields: tuple[str, ...], with_kind: bool) -> list[str]:
        meta = []
        if with_kind:
            meta.append(it.get("kind") or "not yet sorted")
        if it.get("owner"):
            meta.append(f"duty position: {md_text(str(it['owner']))}")
        if it.get("at"):
            meta.append(f"at {_clock(int(it['at']))}")
        block = ["", f"### {n}. {md_text(str(it.get('observation') or '')) or '(no observation)'}", ""]
        if meta:
            block += ["; ".join(meta), ""]
        for f in fields:
            if it.get(f):
                block += [f"**{f.capitalize()}.** {md_text(str(it[f]))}", ""]
        return block

    if shape == "sustain-improve":
        groups = (("Sustain", "sustain"), ("Improve", "improve"), ("Not yet sorted", ""))
        n = 0
        for heading, kind in groups:
            chosen = [it for it in items if (it.get("kind") or "") == kind]
            if not chosen and kind == "":
                continue
            lines += ["", f"## {heading}", ""]
            if not chosen:
                lines.append("- none")
            for it in chosen:
                n += 1
                lines += item_block(n, it, ("discussion", "recommendation"), with_kind=False)
    else:
        lines += ["", "## Record", ""]
        if not items:
            lines.append("- no items")
        for n, it in enumerate(items, 1):
            lines += item_block(n, it, ("discussion", "conclusion", "recommendation"), with_kind=True)
    return "\n".join(lines).rstrip() + "\n"
