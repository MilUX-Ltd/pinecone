"""Spec 017: a transaction that commits late is still recorded.

The recorder reads the server's table forward by id. PostgreSQL allocates an id when a row is
inserted and makes it visible when its transaction commits, so a long transaction can insert a low
id and commit after a shorter one that took a higher id already has. Reading strictly forward
steps over that row for good.

Slice 1a bounded the hole with a fixed lag of 500 ids and threat note 003 recorded the bound as
the residual risk: a commit landing more than 500 ids below the mark is lost silently and
permanently, and nothing detects it. On a busy server 500 ids is seconds.

This closes it with the server's own answer. `pg_snapshot_xmin(pg_current_snapshot())` is the
oldest transaction still in flight; every transaction below it has finished. Recording that
horizon each pass and re-reading anything whose `xmin` is at or above the previous pass's horizon
picks up a late commit however far below the cursor its id fell, and stops re-reading it as soon
as the horizon moves past.

No PostgreSQL here: the source is injected and the horizon is a number the test controls. The
live proof is a real server, and it is spec 017's non-test criterion.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from conftest import load


def row(i: int, secs: int = 0) -> dict[str, Any]:
    t = f"2026-09-13 09:{secs // 60:02d}:{secs % 60:02d}+00"
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
        "groups": "MilUX",
    }


class Server:
    """A source that behaves the way PostgreSQL does about visibility.

    Every row carries the transaction that inserted it. A row is returned when its id is above the
    cursor, or when the caller asks for transactions at or above a horizon, which is how a late
    commit is meant to be found after its id has been passed.
    """

    def __init__(self) -> None:
        self.rows: list[tuple[dict[str, Any], int]] = []  # (row, xmin)
        self.horizon = 1  # the oldest transaction still in flight
        self.asked: list[tuple[int, int]] = []  # (after, since_xid) per call

    def commit(self, r: dict[str, Any], xmin: int) -> None:
        self.rows.append((r, xmin))

    def read(self, after: int, limit: int, since_xid: int = 0) -> list[dict[str, Any]]:
        self.asked.append((after, since_xid))
        out = [r for r, x in self.rows if r["id"] > after or (since_xid and x >= since_xid)]
        return sorted({r["id"]: r for r in out}.values(), key=lambda r: r["id"])[:limit]


def test_a_commit_that_lands_below_the_cursor_is_still_recorded(tmp_path: Path) -> None:
    """Criterion 1, and the whole point of the card.

    A transaction opens, takes id 10, and stays open. A second transaction takes id 900 and
    commits, so the recorder reads it and its cursor goes to 900. The first then commits. Its id is
    890 below the mark, well outside the 500 the fixed lag covers, and today it is lost.
    """
    recorder = load("pinecone_recorder")
    archive = load("pinecone_archive")
    a = archive.Archive(str(tmp_path / "a.db"))
    server = Server()

    server.horizon = 100  # transaction 10 is in flight, so the horizon has not passed it
    server.commit(row(900, secs=30), xmin=200)
    first = recorder.poll_once(server.read, a, free_bytes=lambda: 10**12, xid_horizon=lambda: server.horizon)
    assert first["recorded"] == 1
    assert a.cursor() == 900

    server.commit(row(10, secs=0), xmin=100)  # the long transaction finally commits
    server.horizon = 300
    second = recorder.poll_once(server.read, a, free_bytes=lambda: 10**12, xid_horizon=lambda: server.horizon)

    assert second["recorded"] == 1, "the late commit was stepped over, which is the defect"
    assert a.count() == 2
    ids = [r["id"] for r in a.window(0, 4_000_000_000_000)]
    assert ids == [10, 900]


def test_the_horizon_is_carried_from_one_pass_to_the_next(tmp_path: Path) -> None:
    """Criterion 2. The pass asks for transactions at or above the horizon the previous pass saw,
    not the one it sees now: a transaction in flight during the last read is exactly what must be
    re-read, and by this read it may already have finished."""
    recorder = load("pinecone_recorder")
    archive = load("pinecone_archive")
    a = archive.Archive(str(tmp_path / "a.db"))
    server = Server()

    server.horizon = 100
    recorder.poll_once(server.read, a, free_bytes=lambda: 10**12, xid_horizon=lambda: server.horizon)
    server.horizon = 250
    recorder.poll_once(server.read, a, free_bytes=lambda: 10**12, xid_horizon=lambda: server.horizon)
    server.horizon = 400
    recorder.poll_once(server.read, a, free_bytes=lambda: 10**12, xid_horizon=lambda: server.horizon)

    assert [since for _after, since in server.asked] == [
        0,
        100,
        250,
    ], "each pass must ask from the previous pass's horizon; the first has none to ask from"
    assert a.get_meta("xid_horizon") == "400"


def test_a_failed_pass_does_not_move_the_horizon(tmp_path: Path) -> None:
    """Criterion 3. If the read failed, the transactions in flight at that moment were never read,
    so the horizon must not advance past them. Advancing it on a failure would lose exactly the
    rows this card exists to keep."""
    recorder = load("pinecone_recorder")
    archive = load("pinecone_archive")
    a = archive.Archive(str(tmp_path / "a.db"))
    server = Server()
    server.horizon = 100
    recorder.poll_once(server.read, a, free_bytes=lambda: 10**12, xid_horizon=lambda: server.horizon)
    assert a.get_meta("xid_horizon") == "100"

    def broken(after: int, limit: int, since_xid: int = 0) -> list[dict[str, Any]]:
        raise recorder.SourceError("the server's table did not answer")

    out = recorder.poll_once(broken, a, free_bytes=lambda: 10**12, xid_horizon=lambda: 999)

    assert out["recording"] is False
    assert a.get_meta("xid_horizon") == "100", "the horizon moved on a pass that read nothing"


def test_a_server_that_cannot_give_a_horizon_behaves_as_before(tmp_path: Path) -> None:
    """Criterion 4. The horizon is an improvement on the fixed lag, not a dependency. A server that
    will not answer for one, an older PostgreSQL or a role without the grant, must go on recording
    exactly as it does today rather than stopping."""
    recorder = load("pinecone_recorder")
    archive = load("pinecone_archive")
    a = archive.Archive(str(tmp_path / "a.db"))
    server = Server()
    server.commit(row(5), xmin=10)

    def no_horizon() -> int:
        raise recorder.SourceError("this server does not answer for a transaction horizon")

    out = recorder.poll_once(server.read, a, free_bytes=lambda: 10**12, xid_horizon=no_horizon)

    assert out["recording"] is True, "a missing horizon stopped the recorder"
    assert out["recorded"] == 1
    assert a.get_meta("xid_horizon") == "", "no horizon was stored, so none is asked for next pass"
    assert server.asked == [(0, 0)], "the read fell back to the id cursor alone"


def test_the_query_asks_the_server_for_its_oldest_transaction_in_flight() -> None:
    """Criterion 5. The horizon comes from the server, and the clause is an OR beside the id
    cursor rather than a replacement for it: widening what is read can never lose a row, and the
    writes are idempotent, so the worst a re-read costs is a duplicate insert that is ignored.

    Asserted against the SQL because CI has no PostgreSQL. Read on a real server, 13 September
    2026: PostgreSQL 18.6, the horizon reads in under a millisecond, and adding the clause to a
    2,000-row batch cost 37 ms against 208,041 rows.
    """
    recorder = load("pinecone_recorder")
    assert "pg_snapshot_xmin(pg_current_snapshot())" in recorder.XID_HORIZON_SQL
    # the epoch is stripped so the horizon is comparable with a row's 32-bit xmin
    assert "4294967296" in recorder.XID_HORIZON_SQL, "the xid8 epoch is not reduced to xid space"
    clause = recorder.late_commit_clause(1234)
    assert "xmin" in clause and "1234" in clause
    assert clause.strip().startswith("OR "), "the clause must widen the id cursor, never replace it"
    assert recorder.late_commit_clause(0) == "", "no horizon means no clause"
