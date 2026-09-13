"""Spec 016, capture first: who was on the net, and which groups a report went to.

Steps 1 to 3 of the perceived-picture finding. The point of doing these three before
the page that uses them is that they are the only part of the record that cannot be caught up
later: the server prunes its own connection log, so an event not taken today may not be there
tomorrow, while the reports have been kept since slice 1.

Nothing here draws a picture or claims what anyone saw. It records what the server holds.

The source is injected, so no PostgreSQL is needed; every row is synthetic. The live proof is the
kit, and it is the non-test criterion. The two facts these tests cannot check, because they are
facts about a real server rather than about this code, are asserted against the query text
instead: that the connection log is read from client_endpoint_event, and that a group bit is
found at `length(groups) - bitpos`. Both were read off a running TAK Server rather than reasoned
about, and the second is the one that fails silently: index a bit vector from the left and every
row comes back with no groups at all, which reads exactly like a server that uses no groups.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from conftest import load


def event(i: int, at: str, kind: str = "Connected", cs: str = "ALPHA", groups: str | None = "MilUX") -> dict[str, Any]:
    return {
        "id": i,
        "servertime": f"2026-09-13 {at}+00",
        "event": kind,
        "callsign": cs,
        "uid": f"UID-{cs}",
        "username": cs.lower(),
        "team": "Cyan",
        "role": "Team Member",
        "client_version": "5.8.0",
        "groups": groups,
    }


def report(i: int, secs: int = 0, groups: str | None = "MilUX") -> dict[str, Any]:
    t = f"2026-09-13 08:{secs // 60:02d}:{secs % 60:02d}+00"
    return {
        "id": i,
        "uid": "ANDROID-1",
        "cot_type": "a-f-G-U-C",
        "how": "m-g",
        "start": t,
        "time": t,
        "stale": t,
        "servertime": t,
        "lat": 51.2,
        "lon": -1.5,
        "point_hae": 95.0,
        "point_ce": 9.0,
        "point_le": 9.0,
        "detail": '<detail><contact callsign="ALPHA"/></detail>',
        "groups": groups,
    }


# Criterion 1: the connection log is recorded, in its own table, once


def test_connection_events_are_recorded_once(tmp_path: Path) -> None:
    archive = load("pinecone_archive")
    a = archive.Archive(str(tmp_path / "a.db"))
    rows = [event(1, "08:00:00"), event(2, "08:05:00", "Disconnected")]

    assert a.record(rows, "connection") == 2
    assert a.record(rows, "connection") == 0, "the same events written twice"
    assert a.count("connection") == 2
    assert a.cursor("connection") == 2


def test_the_connection_cursor_is_its_own(tmp_path: Path) -> None:
    """The ids collide with the reports': both start at 1 in their own table on the server. A
    shared cursor would make one table's progress silently skip the other's rows."""
    archive = load("pinecone_archive")
    a = archive.Archive(str(tmp_path / "a.db"))
    a.record([report(i) for i in (1, 2, 3)], "report")
    a.record([event(1, "08:00:00")], "connection")

    assert a.cursor("report") == 3
    assert a.cursor("connection") == 1, "the connection cursor followed the reports'"
    assert a.count("report") == 3
    assert a.count("connection") == 1


def test_what_the_server_holds_is_kept_as_it_is(tmp_path: Path) -> None:
    """Including the shapes a tidy-minded recorder would drop. A real server's log carries a
    Disconnected with no matching Connected, and two Disconnected in a row; both are the record."""
    archive = load("pinecone_archive")
    a = archive.Archive(str(tmp_path / "a.db"))
    a.record(
        [
            event(1, "08:00:00", "Disconnected"),
            event(2, "08:01:00", "Disconnected"),
            event(3, "08:02:00", "Connected"),
        ],
        "connection",
    )

    got = a.connections(_ms("08:00:00") - 1000, _ms("08:03:00"))
    assert [r["event"] for r in got] == ["Disconnected", "Disconnected", "Connected"]
    assert got[0]["callsign"] == "ALPHA" and got[0]["team"] == "Cyan" and got[0]["role"] == "Team Member"
    assert got[0]["client_version"] == "5.8.0"


def test_a_connection_event_is_not_a_report(tmp_path: Path) -> None:
    """The window the player reads is reports and chat. A connection event answers a different
    question and must never arrive as though it were a position on the map."""
    archive = load("pinecone_archive")
    a = archive.Archive(str(tmp_path / "a.db"))
    a.record([report(1)], "report")
    a.record([event(1, "08:00:00")], "connection")

    window = a.window(_ms("07:59:00"), _ms("08:10:00"))
    assert len(window) == 1
    assert all("event" not in r or r.get("cot_type") for r in window)


# Criterion 2: the groups a report was routed to are kept


def test_the_groups_a_report_went_to_are_kept(tmp_path: Path) -> None:
    archive = load("pinecone_archive")
    a = archive.Archive(str(tmp_path / "a.db"))
    a.record([report(1, groups="MilUX"), report(2, secs=10, groups="MilUX,Hackathon")], "report")

    rows = a.window(_ms("07:59:00"), _ms("08:10:00"))
    assert [r["groups"] for r in rows] == ["MilUX", "MilUX,Hackathon"]


def test_unknown_membership_is_null_and_not_an_empty_group(tmp_path: Path) -> None:
    """ "We do not know" and "no groups" are different answers. A report recorded before this
    release carries null and the picture must be able to say membership unknown for it."""
    archive = load("pinecone_archive")
    a = archive.Archive(str(tmp_path / "a.db"))
    a.record([report(1, groups=None)], "report")

    assert a.window(_ms("07:59:00"), _ms("08:10:00"))[0]["groups"] is None


def test_an_archive_written_before_this_release_gains_the_column(tmp_path: Path) -> None:
    """Every box in the field has an archive that predates the column. CREATE TABLE IF NOT EXISTS
    does nothing to a table that is already there, so without a migration the recorder would write
    to a column that does not exist and stop recording on exactly the boxes that matter."""
    path = tmp_path / "old.db"
    old = sqlite3.connect(str(path))
    old.executescript(
        "CREATE TABLE report (id INTEGER PRIMARY KEY, uid TEXT NOT NULL, cot_type TEXT, how TEXT,"
        " device_time TEXT, device_start TEXT, stale TEXT, servertime TEXT NOT NULL, arrived TEXT NOT NULL,"
        " lat REAL, lon REAL, hae REAL, ce REAL, le REAL, detail TEXT);"
        "CREATE TABLE chat (id INTEGER PRIMARY KEY, uid TEXT NOT NULL, cot_type TEXT, how TEXT,"
        " device_time TEXT, device_start TEXT, stale TEXT, servertime TEXT NOT NULL, arrived TEXT NOT NULL,"
        " lat REAL, lon REAL, hae REAL, ce REAL, le REAL, detail TEXT);"
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);"
        "INSERT INTO report (id, uid, servertime, arrived) VALUES (1, 'OLD-1', '2026-09-13 08:00:00+00', '2026-09-13 08:00:01+00');"
    )
    old.commit()
    old.close()

    archive = load("pinecone_archive")
    a = archive.Archive(str(path))

    assert a.record([report(2, secs=30, groups="MilUX")], "report") == 1
    rows = a.window(_ms("07:59:00"), _ms("08:10:00"))
    assert [r["groups"] for r in rows] == [None, "MilUX"], "the old row is unknown, the new one is known"
    a.record([event(1, "08:00:00")], "connection")
    assert a.count("connection") == 1, "and the new table is there too"


# Criterion 3: the query asks the server the right question


def test_the_connection_log_is_read_from_the_servers_own_tables() -> None:
    recorder = load("pinecone_recorder")
    sql = recorder.CONNECTION_SQL
    for table in ("client_endpoint_event", "client_endpoint", "connection_event_type", "groups"):
        assert table in sql, table
    assert recorder.SOURCE_TABLE["connection"] == "client_endpoint_event"


def test_a_group_bit_is_counted_from_the_right_hand_end() -> None:
    """The fact that fails silently. TAK Server's groups bit vector is 32768 bits wide and
    `groups.bitpos` counts from the right, so the offset is `length(groups) - bitpos`. Indexed
    from the left it matches nothing, every row comes back with no groups, and that is
    indistinguishable from a server that does not use groups. Read on a running server: the
    group at bitpos 3 was found at text position 32765 of a 32768-bit vector.
    """
    recorder = load("pinecone_recorder")
    expr = recorder.GROUP_NAMES.format(alias="x")
    assert "length(x.groups) - g.bitpos" in expr, "the offset must be from the right-hand end"
    assert "g.name" in expr and "groups g" in expr, "the names come from the server's own groups table"
    assert "x.groups IS NOT NULL" in expr, "no bits set is not the same as no vector"


def test_the_recorder_always_reads_the_connection_log() -> None:
    """Even with chat turned off. --no-chat turns off a kind of CoT message; it does not turn off
    the record of who was on the net, and that is the one source the server deletes behind us.

    Reached through all_sources rather than sources: the type filter has no opinion about a
    connection event, and spec 008's contract for sources() is that it returns the CoT tables the
    filter chose. Widening that function instead would have quietly changed what --no-chat means.
    """
    recorder = load("pinecone_recorder")
    assert [n for n, _ in recorder.all_sources(("a-",))] == ["report", "connection"]
    assert [n for n, _ in recorder.all_sources(("a-", "b-t-f"))] == ["report", "chat", "connection"]
    assert [n for n, _ in recorder.sources(("a-",))] == ["report"], "spec 008's contract is untouched"


def test_a_pass_over_the_connection_log_records_and_advances(tmp_path: Path) -> None:
    """The whole poll machinery, cursor, floor, heartbeat and all, works on the new table without
    a second copy of it: that was the point of giving the archive one record() with two shapes."""
    recorder = load("pinecone_recorder")
    archive = load("pinecone_archive")
    a = archive.Archive(str(tmp_path / "a.db"))
    events = [event(i, f"08:{i:02d}:00") for i in range(1, 6)]

    out = recorder.poll_once(
        lambda after, limit: [e for e in events if e["id"] > after][:limit],
        a,
        free_bytes=lambda: 10**12,
        table="connection",
        head_id=lambda: 5,
    )

    assert out["recording"] is True
    assert out["recorded"] == 5
    assert a.count("connection") == 5
    assert a.get_meta("connection_recording") == "yes", "the connection table's state keys are prefixed"


def _ms(at: str) -> int:
    import calendar
    import time

    return calendar.timegm(time.strptime(f"2026-09-13 {at}", "%Y-%m-%d %H:%M:%S")) * 1000


# The regression found on a real box, 13 September 2026


def test_reports_still_record_when_the_groups_table_cannot_be_read(monkeypatch) -> None:
    """Found live, and it was a real defect rather than a hypothetical.

    The group-names subquery reads the server's `groups` table. Every box in the field runs a role
    with no grant on it until it updates, and on such a box the whole report query failed with
    "permission denied for table groups": not a missing groups column, but reports and chat not
    recording at all. Verified on a real server before the fix and after it.

    A box that cannot read the group names records reports with no groups, which the archive
    already defines as membership unknown. It must not stop recording.

    Learned from the refusal rather than probed, so a role that has the grant pays no extra round
    trip. The first attempt carries the subquery; only the retry drops it.
    """
    recorder = load("pinecone_recorder")
    seen: list[str] = []

    def fake_run(argv, **kw):
        sql = argv[-1]
        seen.append(sql)
        if "FROM groups g" in sql:
            return type(
                "R", (), {"returncode": 1, "stdout": "", "stderr": "ERROR:  permission denied for table groups"}
            )()
        return type("R", (), {"returncode": 0, "stdout": "id,uid,cot_type\n", "stderr": ""})()

    monkeypatch.setattr(recorder.subprocess, "run", fake_run)
    rows = recorder.psql_source("cot_router")(0, 10)

    assert rows == [], "the retry did not return the server's answer"
    assert len(seen) == 2, "expected one refused attempt and one retry"
    assert "FROM groups g" in seen[0], "the first attempt should ask for the names"
    assert "FROM groups g" not in seen[1], "the retry still joins a table this role cannot read"
    assert "NULL AS groups" in seen[1], "the column must still be selected, as null, so the shape holds"


def test_a_role_that_can_read_groups_pays_nothing_extra(monkeypatch) -> None:
    """The control. With the grant there is one query and it carries the names."""
    recorder = load("pinecone_recorder")
    seen: list[str] = []

    def fake_run(argv, **kw):
        seen.append(argv[-1])
        return type("R", (), {"returncode": 0, "stdout": "id,uid,cot_type\n", "stderr": ""})()

    monkeypatch.setattr(recorder.subprocess, "run", fake_run)
    recorder.psql_source("cot_router")(0, 10)

    assert len(seen) == 1, "a role with the grant should make exactly one call"
    assert "FROM groups g" in seen[0]


def test_the_connection_log_says_what_to_do_when_it_is_not_granted(monkeypatch) -> None:
    """The message an operator actually meets on a box whose role predates the grants. It fails
    before the read, at the head-id call, where the generic wording says nothing useful."""
    recorder = load("pinecone_recorder")

    def fake_run(argv, **kw):
        return type("R", (), {"returncode": 1, "stdout": "", "stderr": "permission denied"})()

    monkeypatch.setattr(recorder.subprocess, "run", fake_run)
    try:
        recorder.head_for("connection")()
    except recorder.SourceError as e:
        assert "four connection tables" in str(e), f"unhelpful: {e}"
        assert "Positions and chat are unaffected" in str(e)
    else:
        raise AssertionError("a refused connection log should raise")
