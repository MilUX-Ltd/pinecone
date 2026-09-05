"""Spec 008, chat on the timeline. GeoChat is `b-t-f` in the same stream; it is recorded beside
the positions and comes out as messages. Every message here is synthetic."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

from conftest import load

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

T0 = 1_788_426_000_000


def stamp(ms: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ms / 1000)) + f".{ms % 1000:03d}+00"


def position(i: int, cs: str, at: int) -> dict[str, Any]:
    return {
        "id": i,
        "uid": f"UID-{cs}",
        "cot_type": "a-f-G-U-C",
        "how": "m-g",
        "start": stamp(at),
        "time": stamp(at),
        "stale": stamp(at + 60_000),
        "servertime": stamp(at + 1_000),
        "lat": 51.2,
        "lon": -1.5,
        "point_hae": 90.0,
        "point_ce": 9.0,
        "point_le": 9.0,
        "detail": f'<detail><contact callsign="{cs}"/><takv platform="ATAK-CIV"/></detail>',
    }


def chat(i: int, sender: str, at: int, text: str, room: str = "All Chat Rooms") -> dict[str, Any]:
    detail = (
        f'<detail><__chat parent="RootContactGroup" groupOwner="false" chatroom="{room}" '
        f'id="{room}" senderCallsign="{sender}"><chatgrp uid0="UID-{sender}" uid1="{room}" id="{room}"/></__chat>'
        f'<link uid="UID-{sender}" type="a-f-G-U-C" relation="p-p"/>'
        f'<remarks source="BAO.F.ATAK.UID-{sender}" to="{room}" time="{stamp(at)}">{text}</remarks></detail>'
    )
    return {
        "id": i,
        "uid": f"GeoChat.UID-{sender}.{room}.{i}",
        "cot_type": "b-t-f",
        "how": "h-g-i-g-o",
        "start": stamp(at),
        "time": stamp(at),
        "stale": stamp(at + 86_400_000),
        "servertime": stamp(at + 2_000),
        "lat": 51.2,
        "lon": -1.5,
        "point_hae": 90.0,
        "point_ce": 9999999.0,
        "point_le": 9999999.0,
        "detail": detail,
    }


def test_chat_is_recorded_beside_positions(tmp_path: Path) -> None:
    """Criterion 1. The recorder's source now asks for b-t-f as well as a-*; the archive keeps them
    as rows with their type, and a position report is still a position report."""
    recorder = load("pinecone_recorder")
    assert "b-t-f" in recorder.SOURCE_TYPES, "the recorder asks the server for chat"
    assert "a-" in recorder.SOURCE_TYPES
    archive = load("pinecone_archive")
    a = archive.Archive(str(tmp_path / "a.db"))
    a.record([position(1, "ALPHA", T0), chat(2, "ALPHA", T0 + 5_000, "moving now"), position(3, "ALPHA", T0 + 30_000)])
    rows = a.window(0, 4 * 10**12)
    assert sorted(r["cot_type"] for r in rows) == ["a-f-G-U-C", "a-f-G-U-C", "b-t-f"]
    assert "moving now" in next(r["detail"] for r in rows if r["cot_type"] == "b-t-f"), "the whole detail is kept"


def test_the_recorder_asks_the_server_for_chat_and_can_be_told_not_to(monkeypatch) -> None:
    """Criterion 1 at the SQL, and the opt-out. The pre-UAT review of slice 5 showed that a mutant
    ignoring the types in the WHERE clause left every test green: SOURCE_TYPES was asserted, the
    query built from it was not. So the query is captured here, from the argv handed to psql, for
    the default and for each way of saying no."""
    recorder = load("pinecone_recorder")
    seen: list[str] = []

    class Done:
        returncode = 0
        stdout = "id,uid,cot_type\n"
        stderr = ""

    def fake_run(argv, **kw):
        seen.append(argv[-1])
        return Done()

    monkeypatch.setattr(recorder.subprocess, "run", fake_run)
    recorder.psql_source()(0, 10)
    assert "cot_type LIKE 'a-%'" in seen[-1] and "cot_type LIKE 'b-t-f%'" in seen[-1], "the default asks for both"
    recorder.psql_source(types=recorder.chosen_types(True, {}))(0, 10)
    assert "b-t-f" not in seen[-1] and "cot_type LIKE 'a-%'" in seen[-1], "--no-chat asks for positions only"
    assert recorder.chosen_types(False, {"PINECONE_CHAT": "no"}) == ("a-",)
    assert recorder.chosen_types(False, {"PINECONE_CHAT": "OFF"}) == ("a-",)
    assert recorder.chosen_types(False, {"PINECONE_CHAT": "yes"}) == recorder.SOURCE_TYPES
    assert recorder.chosen_types(False, {}) == recorder.SOURCE_TYPES
    assert recorder.SOURCE_TYPES == ("a-", "b-t-f"), "and the constant is not what main() reassigns"


def test_chat_is_recorded_from_the_servers_chat_table(tmp_path: Path) -> None:
    """Criterion 1 on the real server. TAK Server 5.8 does not put GeoChat in cot_router at all: on
    a real server there are no b-t-f rows in cot_router and 125 in cot_router_chat, a table with the same
    columns and its own id sequence (read on 5 September 2026). A recorder that asked cot_router for
    b-t-f recorded no chat on a real server while every test passed on synthetic rows.

    So the archive keeps chat in a table of its own with its own cursor, because the two id
    sequences collide, and a window reads both, merged in the order the server received them."""
    archive = load("pinecone_archive")
    recorder = load("pinecone_recorder")
    a = archive.Archive(str(tmp_path / "a.db"))
    reports = [position(5, "ALPHA", T0), position(6, "ALPHA", T0 + 30_000)]
    messages = [
        chat(5, "ALPHA", T0 + 5_000, "moving now"),
        chat(6, "BRAVO", T0 + 20_000, "roger"),
    ]  # ids collide with the reports'

    def report_source(after: int, limit: int) -> list[dict[str, Any]]:
        return [r for r in reports if int(r["id"]) > after][:limit]

    def chat_source(after: int, limit: int) -> list[dict[str, Any]]:
        return [r for r in messages if int(r["id"]) > after][:limit]

    assert recorder.poll_once(report_source, a)["recorded"] == 2
    assert recorder.poll_once(chat_source, a, table="chat")["recorded"] == 2
    assert a.stats()["count"] == 2 and a.stats()["messages"] == 2, "reports and messages counted apart"
    assert a.cursor() == 6 and a.cursor("chat") == 6, "each table has its own cursor"
    rows = a.window(0, 4 * 10**12)
    assert [r["cot_type"] for r in rows] == ["a-f-G-U-C", "b-t-f", "b-t-f", "a-f-G-U-C"], "merged in servertime order"
    assert a.count_window(0, 4 * 10**12) == 4
    b = a.bundle(T0 - 1, T0 + 10**7)
    assert [m["text"] for m in b["chat"]] == ["moving now", "roger"]
    assert recorder.poll_once(chat_source, a, table="chat")["recorded"] == 0, "and not written twice"
    assert recorder.poll_once(report_source, a)["recorded"] == 0


def test_the_recorder_reads_two_tables_and_can_be_told_to_read_one(monkeypatch) -> None:
    """The sources a run reads, by name and by the query each hands to psql."""
    recorder = load("pinecone_recorder")
    seen: list[str] = []

    class Done:
        returncode = 0
        stdout = "id,uid,cot_type\n"
        stderr = ""

    def fake_run(argv, **kw):
        seen.append(argv[-1])
        return Done()

    monkeypatch.setattr(recorder.subprocess, "run", fake_run)
    both = recorder.sources(recorder.chosen_types(False, {}))
    assert [name for name, _ in both] == ["report", "chat"]
    for _, src in both:
        src(0, 10)
    assert "FROM cot_router " in seen[0] and "cot_type LIKE 'a-%'" in seen[0]
    assert "FROM cot_router_chat " in seen[1] and "cot_type LIKE 'b-t-f%'" in seen[1]
    only = recorder.sources(recorder.chosen_types(True, {}))
    assert [name for name, _ in only] == ["report"], "--no-chat reads one table and never asks for the other"


def test_chat_comes_out_as_messages_not_positions() -> None:
    """Criterion 2."""
    import build_bundle

    rows = [
        position(1, "ALPHA", T0),
        position(2, "BRAVO", T0),
        chat(3, "ALPHA", T0 + 5_000, "moving now"),
        chat(4, "BRAVO", T0 + 20_000, "roger, hold at the bridge", room="Cyan"),
        position(5, "ALPHA", T0 + 30_000),
    ]
    b = build_bundle.bundle_from_rows(rows, start_ms=T0 - 1, end_ms=T0 + 10**7)
    assert {t["callsign"] for t in b["tracks"]} == {"ALPHA", "BRAVO"}
    assert all(t["n"] in (1, 2) for t in b["tracks"]), "no chat event became a position"
    msgs = b["chat"]
    assert [m["text"] for m in msgs] == ["moving now", "roger, hold at the bridge"]
    assert msgs[0]["sender"] == "ALPHA" and msgs[0]["room"] == "All Chat Rooms"
    assert msgs[1]["sender"] == "BRAVO" and msgs[1]["room"] == "Cyan"
    assert len(b["chat"]) == 2
    assert b["counts"]["rows_kept"] == 5, "messages count as rows kept, so the four counts still reconcile"


def test_a_message_keeps_every_timestamp() -> None:
    import build_bundle

    b = build_bundle.bundle_from_rows([chat(1, "ALPHA", T0, "hello")], start_ms=T0 - 1, end_ms=T0 + 10**7)
    m = b["chat"][0]
    assert m["servertime"] == T0 + 2_000
    assert m["time"] == T0, "the device's own time"
    assert m["servertime"] - m["time"] == 2_000, "so its latency is readable too"


def test_a_malformed_message_is_kept_not_dropped() -> None:
    """Criterion 3. A chat event with no remarks, or no sender, is what arrived; it is listed with
    what it has and never crashes the bundle."""
    import build_bundle

    bare = chat(1, "ALPHA", T0, "x")
    bare["detail"] = '<detail><__chat chatroom="All Chat Rooms"/></detail>'
    b = build_bundle.bundle_from_rows([bare], start_ms=T0 - 1, end_ms=T0 + 10**7)
    m = b["chat"][0]
    assert m["text"] == "" and m["sender"] == "" and m["room"] == "All Chat Rooms"
    assert m["uid"], "it still says which event it was"


def test_a_message_with_a_newline_and_an_escaped_sender_is_kept_whole() -> None:
    """A message can carry a newline, and the regex that reads it runs across lines; a sender or a
    room is unescaped the way the text is, so an ampersand in a callsign comes back as one."""
    import build_bundle

    row = chat(1, "A&amp;B", T0, "first line\nsecond line", room="Ops &amp; Plans")
    b = build_bundle.bundle_from_rows([row], start_ms=T0 - 1, end_ms=T0 + 10**7)
    m = b["chat"][0]
    assert m["text"] == "first line\nsecond line"
    assert m["sender"] == "A&B" and m["room"] == "Ops & Plans"


def test_message_text_is_kept_exactly() -> None:
    """Criterion 5, the server half. The page renders it as text; that half is a later slice.

    What the person typed, and what the XML carries it as: markup in a message is escaped on the
    wire, and "kept exactly" means the typed text comes back, brackets and all, for the page to
    render as text."""
    import build_bundle

    typed = "<b>bold</b> & <img src=x onerror=alert(1)>"
    on_the_wire = "&lt;b&gt;bold&lt;/b&gt; &amp; &lt;img src=x onerror=alert(1)&gt;"
    b = build_bundle.bundle_from_rows([chat(1, "ALPHA", T0, on_the_wire)], start_ms=T0 - 1, end_ms=T0 + 10**7)
    assert b["chat"][0]["text"] == typed
