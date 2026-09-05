"""Spec 010, the structured record: one debrief's record, in the unit's own shape, kept on the box
and taken away as a file. Everything here is synthetic."""

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

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

T0 = 1_788_426_000_000
T1 = T0 + 2 * 60 * 60_000


def call(url: str, data: dict[str, str] | None = None) -> tuple[int, str, dict[str, str]]:
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode(), {k.lower(): v for k, v in r.headers.items()}
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), {k.lower(): v for k, v in e.headers.items()}


def start(port: int, data: Path, state: Path, env: dict[str, str] | None = None) -> subprocess.Popen:
    e = {k: v for k, v in os.environ.items() if not k.startswith("PINECONE_")}
    e.update(env or {})
    p = subprocess.Popen(
        [sys.executable, str(ROOT / "serve.py"), "--port", str(port), "--data", str(data), "--state", str(state)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=e,
    )
    for _ in range(60):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/version", timeout=1).read()
            break
        except Exception:
            time.sleep(0.1)
    return p


def make_box(tmp_path: Path, env: dict[str, str] | None = None, band: int = 9800):
    """A server of its own. Two boxes in one test take two bands, or the second fails to bind and
    every call quietly reaches the first, which is how the shape test first passed the wrong box."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    port = band + (os.getpid() % 50)  # 9800 and 9850: bands of their own, beside spec 006's and spec 009's
    p = start(port, data, state, env)
    return p, f"http://127.0.0.1:{port}", state


@pytest.fixture()
def box(tmp_path: Path):
    p, base, state = make_box(tmp_path)
    try:
        yield base, state
    finally:
        p.terminate()
        p.wait(timeout=5)


def new_record(
    base: str, title: str = "Ex GOLDEN PINE, serial 3", objectives: str = "Secure the crossing by 0900."
) -> dict:
    code, body, _ = call(
        f"{base}/api/records", {"title": title, "start": str(T0), "end": str(T1), "objectives": objectives}
    )
    assert code == 200, body
    return json.loads(body)


ITEM = {
    "observation": "Passage of lines at the bridge took forty minutes against a plan of ten.",
    "discussion": "The handover point was not agreed before H hour and both callsigns waited for the other.",
    "conclusion": "The passage plan lacked a named handover point and a named time.",
    "recommendation": "Name the handover point and the time in the orders and rehearse it once.",
    "kind": "improve",
    "owner": "Platoon commander",
    "at": str(T0 + 25 * 60_000),
}


# ---- criterion 1 ----------------------------------------------------------------------------------------


def test_a_record_is_made_for_a_window_and_kept_on_the_box(box) -> None:
    base, state = box
    made = new_record(base)
    assert made["id"] and made["title"].startswith("Ex GOLDEN PINE")
    assert made["window"] == {"start": T0, "end": T1}
    listed = json.loads(call(f"{base}/api/records")[1])
    assert [r["id"] for r in listed["records"]] == [made["id"]]
    assert listed["records"][0]["items"] == 0 and listed["records"][0]["objectives"].startswith("Secure")
    assert listed["cap"] == 50
    assert (state / "records.json").exists(), "kept on the box, beside the moments"

    code, body, _ = call(f"{base}/api/records/{made['id']}", {"title": "Ex GOLDEN PINE, serial 3, company AAR"})
    assert code == 200 and json.loads(body)["title"].endswith("company AAR")
    code, body, _ = call(f"{base}/api/records", {"title": "x" * 201, "start": str(T0), "end": str(T1)})
    assert code == 400 and "200" in body
    code, body, _ = call(f"{base}/api/records", {"title": "backwards", "start": str(T1), "end": str(T0)})
    assert code == 400
    code, body, _ = call(f"{base}/api/records/{made['id']}/delete", {})
    assert code == 200
    assert json.loads(call(f"{base}/api/records")[1])["records"] == []


# ---- criterion 2 ----------------------------------------------------------------------------------------


def test_an_item_is_odcr_shaped_with_a_kind_and_an_owner(box) -> None:
    base, _ = box
    rid = new_record(base)["id"]
    code, body, _ = call(f"{base}/api/records/{rid}/items", ITEM)
    assert code == 200, body
    item = json.loads(body)
    for k in ("observation", "discussion", "conclusion", "recommendation", "kind", "owner"):
        assert item[k] == ITEM[k]
    assert item["at"] == T0 + 25 * 60_000 and item["id"]
    got = json.loads(call(f"{base}/api/records/{rid}")[1])
    assert [i["id"] for i in got["items"]] == [item["id"]]

    code, body, _ = call(f"{base}/api/records/{rid}/items", {**ITEM, "observation": "   "})
    assert code == 400 and "observation" in body
    code, body, _ = call(f"{base}/api/records/{rid}/items", {**ITEM, "kind": "blame"})
    assert code == 400 and "sustain" in body and "improve" in body
    code, body, _ = call(f"{base}/api/records/{rid}/items", {**ITEM, "owner": "o" * 81})
    assert code == 400 and "80" in body

    code, body, _ = call(
        f"{base}/api/records/{rid}/items/{item['id']}", {"kind": "sustain", "recommendation": "Keep it."}
    )
    assert code == 200 and json.loads(body)["kind"] == "sustain" and json.loads(body)["recommendation"] == "Keep it."
    code, body, _ = call(f"{base}/api/records/{rid}/items/{item['id']}/delete", {})
    assert code == 200 and json.loads(call(f"{base}/api/records/{rid}")[1])["items"] == []


# ---- criterion 3 ----------------------------------------------------------------------------------------


def test_the_budget_is_twelve_with_the_count_visible(box) -> None:
    base, _ = box
    rid = new_record(base)["id"]
    for i in range(12):
        code, body, _ = call(f"{base}/api/records/{rid}/items", {**ITEM, "observation": f"Observation {i + 1}"})
        assert code == 200, body
    code, body, _ = call(f"{base}/api/records/{rid}/items", {**ITEM, "observation": "One too many"})
    assert code == 409 and "12" in body
    got = json.loads(call(f"{base}/api/records/{rid}")[1])
    assert got["items_cap"] == 12 and got["doctrine"] == {"sustain": 3, "improve": 3}
    assert len(got["items"]) == 12


# ---- criterion 4 ----------------------------------------------------------------------------------------


def test_the_export_is_markdown_in_the_chosen_shape(box, tmp_path: Path) -> None:
    base, _ = box
    rid = new_record(base)["id"]
    call(f"{base}/api/moments", {"at": str(T0 + 25 * 60_000), "name": "passage of lines, bridge"})
    mid = json.loads(call(f"{base}/api/moments")[1])["moments"][0]["id"]
    call(f"{base}/api/moments/{mid}", {"promoted": "yes"})
    call(f"{base}/api/moments", {"at": str(T1 + 60_000), "name": "outside the window"})
    call(f"{base}/api/records/{rid}/items", ITEM)
    call(
        f"{base}/api/records/{rid}/items",
        {**ITEM, "kind": "sustain", "observation": "The casevac drill ran as rehearsed."},
    )

    code, text, headers = call(f"{base}/record/{rid}.md")
    assert code == 200, text
    assert headers["content-type"].startswith("text/markdown")
    assert "attachment" in headers.get("content-disposition", "")
    assert headers.get("x-content-type-options") == "nosniff"
    assert text.startswith("# Ex GOLDEN PINE")
    assert "Secure the crossing by 0900." in text
    assert "passage of lines, bridge" in text and "outside the window" not in text
    assert "Conclusion" in text and ITEM["conclusion"] in text and ITEM["recommendation"] in text
    assert "Platoon commander" in text and "improve" in text
    assert "Contains callsigns" in text and "Pinecone" in text
    assert "## Sustain" not in text

    # the other shape, from the same store
    p, base2, _ = make_box(tmp_path / "two", {"PINECONE_RECORD": "sustain-improve"}, band=9850)
    try:
        rid2 = new_record(base2)["id"]
        call(f"{base2}/api/records/{rid2}/items", ITEM)
        call(
            f"{base2}/api/records/{rid2}/items",
            {**ITEM, "kind": "sustain", "observation": "The casevac drill ran as rehearsed."},
        )
        code, text, _ = call(f"{base2}/record/{rid2}.md")
        assert code == 200
        assert "## Sustain" in text and "## Improve" in text
        assert text.index("## Sustain") < text.index("## Improve")
        assert "The casevac drill ran as rehearsed." in text and ITEM["observation"] in text
        assert "Conclusion" not in text and ITEM["conclusion"] not in text
        assert json.loads(call(f"{base2}/api/records")[1])["shape"] == "sustain-improve"
    finally:
        p.terminate()
        p.wait(timeout=5)


# ---- criterion 5 ----------------------------------------------------------------------------------------


def test_an_item_from_a_moment_carries_only_what_the_moment_said(box) -> None:
    base, _ = box
    rid = new_record(base)["id"]
    call(f"{base}/api/moments", {"at": str(T0 + 25 * 60_000), "name": "passage of lines, bridge"})
    mid = json.loads(call(f"{base}/api/moments")[1])["moments"][0]["id"]
    code, body, _ = call(f"{base}/api/records/{rid}/items", {"moment": mid})
    assert code == 200, body
    item = json.loads(body)
    assert item["observation"] == "passage of lines, bridge" and item["at"] == T0 + 25 * 60_000
    assert item["discussion"] == "" and item["conclusion"] == "" and item["recommendation"] == ""
    assert item["owner"] == "" and item["kind"] == "", "not yet sorted: sorting it would be the tool's judgement"
    text = call(f"{base}/record/{rid}.md")[1]
    assert "## ALPHA" not in text and "per callsign" not in text.lower()
    for word in ("cause", "fault", "rating", "score", "why"):
        assert word not in item, "the tool has no field for a judgement"
    code, body, _ = call(f"{base}/api/records/{rid}/items", {"moment": "ffffffffffff"})
    assert code == 404


# ---- criterion 6 ----------------------------------------------------------------------------------------


def test_text_is_text_and_the_store_degrades(box) -> None:
    base, state = box
    title = "<script>alert(1)</script> & co"
    rid = new_record(base, title=title)["id"]
    call(f"{base}/api/records/{rid}/items", {**ITEM, "discussion": "# not a heading\n- not a list\n> not a quote"})
    got = json.loads(call(f"{base}/api/records/{rid}")[1])
    assert got["title"] == title, "verbatim in JSON; the page binds it as text"
    text = call(f"{base}/record/{rid}.md")[1]
    assert "\\<script>" in text and "<script>" not in text.replace("\\<script>", ""), "every tag escaped, nothing more"
    assert "\n# not a heading" not in text and "\\# not a heading" in text
    assert "\n- not a list" not in text and "\n> not a quote" not in text
    assert ITEM["observation"] in text

    store = state / "records.json"
    good = json.loads(store.read_text())
    damaged = ["a string", None, {"id": 7}, {"id": "abc", "title": None}, {**good[0], "items": "nope"}, *good]
    store.write_text(json.dumps(damaged))
    listed = json.loads(call(f"{base}/api/records")[1])["records"]
    assert [r["id"] for r in listed] == [rid], "what can be read is read; the rest is dropped"
    assert listed[0]["items"] == 1


# ---- the slice 7 review: findings and surviving mutants, each held by a test ---------------------------


def test_images_links_setext_headings_and_rules_read_as_text(box) -> None:
    """Review finding 1. A moment named by a handset reaches the export with one press, and the
    first escape left an image live: a fetch from wherever the record is opened."""
    base, _ = box
    rid = new_record(
        base,
        objectives="Secure the crossing by 0900\n---\ntext\n===\n***\n___\n-> arrow, 5 > 3, #hashtag, -5 degrees, 1.5 litres, AT&T",
    )["id"]
    call(
        f"{base}/api/moments",
        {"at": str(T0 + 60_000), "name": "![x](http://example.invalid/pixel.png) with [ALPHA](javascript:alert(1))"},
    )
    mid = json.loads(call(f"{base}/api/moments")[1])["moments"][0]["id"]
    call(f"{base}/api/moments/{mid}", {"promoted": "yes"})
    text = call(f"{base}/record/{rid}.md")[1]
    assert "![x]" not in text and "!\\[x]" in text
    assert "\\[ALPHA]" in text and "[ALPHA]" not in text.replace("\\[ALPHA]", ""), "every bracket escaped"
    assert "\n---\n" not in text and "\n\\---" in text, "a thematic break, and a setext underline, escaped"
    assert "\n===\n" not in text and "\n\\===" in text
    assert "\n***\n" not in text and "\n___\n" not in text
    assert (
        "-> arrow, 5 > 3, #hashtag, -5 degrees, 1.5 litres, AT&T" in text
    ), "mid-line > and harmless leads are left as typed"


def test_an_oversize_body_is_refused_with_a_sentence(box) -> None:
    """Review finding 4: four fields of 4,000 non-ASCII characters exceeded the read cap on the
    wire and were truncated with a 200."""
    base, _ = box
    rid = new_record(base)["id"]
    big = "é" * 4000
    code, body, _ = call(
        f"{base}/api/records/{rid}/items",
        {**ITEM, "observation": big, "discussion": big, "conclusion": big, "recommendation": big},
    )
    assert code == 413 and "long" in body.lower()
    assert json.loads(call(f"{base}/api/records/{rid}")[1])["items"] == [], "nothing half-kept"
    req = urllib.request.Request(f"{base}/api/records/{rid}/items", data=b"observation=x", method="POST")
    req.add_header("Content-Length", "not a number")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
    except Exception:
        code = 0
    assert code == 400, "a malformed length is a sentence, not a traceback"


def test_the_index_is_the_promoted_moments_and_the_limits_hold(box) -> None:
    """Review note 5: the export ignored `promoted` and no test noticed; the fifty-first record,
    objectives over the limit and an unknown shape were held by nothing."""
    base, state = box
    rid = new_record(base)["id"]
    call(f"{base}/api/moments", {"at": str(T0 + 60_000), "name": "kept but not promoted"})
    call(f"{base}/api/moments", {"at": str(T0 + 120_000), "name": "promoted one"})
    pid = next(m["id"] for m in json.loads(call(f"{base}/api/moments")[1])["moments"] if m["name"] == "promoted one")
    call(f"{base}/api/moments/{pid}", {"promoted": "yes"})
    text = call(f"{base}/record/{rid}.md")[1]
    assert "promoted one" in text and "kept but not promoted" not in text
    code, body, _ = call(
        f"{base}/api/records", {"title": "t", "start": str(T0), "end": str(T1), "objectives": "o" * 4001}
    )
    assert code == 400 and "4000" in body
    for i in range(49):
        assert call(f"{base}/api/records", {"title": f"record {i}", "start": str(T0), "end": str(T1)})[0] == 200
    code, body, _ = call(f"{base}/api/records", {"title": "one too many", "start": str(T0), "end": str(T1)})
    assert code == 409 and "50" in body
    import pinecone_record as pr

    assert pr.chosen_shape({"PINECONE_RECORD": "sustain_improve"}) == "odcr", "an unknown value reads as the default"
    assert pr.chosen_shape({"PINECONE_RECORD": " SUSTAIN-IMPROVE "}) == "sustain-improve"


def test_a_stored_item_without_an_observation_is_kept_with_what_it_has() -> None:
    """Review note 13: the store dropped an item whose observation a hand edit had blanked, losing
    the discussion the room typed. Degrade, as the moments and the chat do."""
    import pinecone_record as pr

    raw = {
        "id": "abcdefabcdef",
        "observation": "",
        "discussion": "the room said this",
        "kind": "improve",
        "owner": "",
        "at": None,
    }
    kept = pr._clean_stored_item(raw)
    assert kept is not None and kept["discussion"] == "the room said this" and kept["observation"] == ""
    text = pr.render_markdown(
        {"id": "r", "title": "t", "window": {"start": T0, "end": T1}, "objectives": "", "items": [kept]},
        [],
        "odcr",
        "box",
        "0.0.0",
        T0,
    )
    assert "(no observation)" in text and "the room said this" in text
