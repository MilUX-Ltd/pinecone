"""Spec 003, criteria 1, 2, 3 and 5: the recorder writes once, resumes, keeps everything, outlives
the source, and stops before it fills the disk. The source is injected, so no PostgreSQL is needed
here; the live proof is a real box."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from conftest import load

DETAIL = (
    '<detail><contact callsign="ALPHA"/><takv device="SM-S911U1" platform="ATAK-CIV" os="33" version="5.8"/>'
    '<__group name="Cyan" role="Team Member"/><track speed="1.4" course="90"/><status battery="61"/></detail>'
)


def row(i: int, uid: str = "ANDROID-1", secs: int = 0, callsign: str = "ALPHA") -> dict[str, Any]:
    t = f"2026-09-03 08:{secs // 60:02d}:{secs % 60:02d}+00"
    return {
        "id": i,
        "uid": uid,
        "cot_type": "a-f-G-U-C",
        "how": "m-g",
        "start": t,
        "time": t,
        "stale": t,
        "servertime": t,
        "lat": 51.213 + i * 1e-5,
        "lon": -1.505 - i * 1e-5,
        "point_hae": 95.0,
        "point_ce": 9.0,
        "point_le": 9.0,
        "detail": DETAIL.replace('callsign="ALPHA"', f'callsign="{callsign}"'),
    }


def test_records_new_reports_once(tmp_path: Path) -> None:
    archive = load("pinecone_archive")
    a = archive.Archive(str(tmp_path / "a.db"))
    assert a.record([row(1), row(2, secs=30)]) == 2
    assert a.record([row(1), row(2, secs=30), row(3, secs=60)]) == 1, "a report already held is not written again"
    assert a.stats()["count"] == 3
    assert a.cursor() == 3


def test_resumes_from_its_cursor_after_a_restart(tmp_path: Path) -> None:
    archive = load("pinecone_archive")
    path = str(tmp_path / "a.db")
    a = archive.Archive(path)
    a.record([row(1), row(2, secs=30)])
    a.close()
    b = archive.Archive(path)
    assert b.cursor() == 2, "it picks up where it left off, not from the beginning"
    assert b.record([row(3, secs=60)]) == 1
    assert b.stats()["count"] == 3


def test_every_timestamp_and_the_whole_detail_are_kept(tmp_path: Path) -> None:
    archive = load("pinecone_archive")
    a = archive.Archive(str(tmp_path / "a.db"))
    a.record([row(1)])
    got = a.window(0, 4102444800000)[0]
    assert got["uid"] == "ANDROID-1" and got["cot_type"] == "a-f-G-U-C" and got["how"] == "m-g"
    for field in ("device_time", "device_start", "stale", "servertime", "arrived"):
        assert got[field], field
    assert got["detail"] == DETAIL, "the blob is kept whole, not parsed away"
    assert abs(float(got["lat"]) - 51.21301) < 1e-6


def test_the_archive_survives_a_purge_of_the_source(tmp_path: Path) -> None:
    archive = load("pinecone_archive")
    recorder = load("pinecone_recorder")
    a = archive.Archive(str(tmp_path / "a.db"))
    source_rows = [row(1), row(2, secs=30)]

    def source(after: int, limit: int) -> list[dict[str, Any]]:
        return [r for r in source_rows if int(r["id"]) > after][:limit]

    assert recorder.poll_once(source, a)["recorded"] == 2
    source_rows.clear()  # TAK's retention service does its worst
    assert recorder.poll_once(source, a)["recorded"] == 0
    assert a.stats()["count"] == 2, "the record is ours, and it stays"


def test_a_window_exports_as_the_bundle_the_player_reads(tmp_path: Path) -> None:
    archive = load("pinecone_archive")
    a = archive.Archive(str(tmp_path / "a.db"))
    a.record([row(1), row(2, secs=30), row(3, uid="!mesh", secs=200, callsign="TRK1")])
    b = a.bundle(0, 4102444800000)
    assert b["format"] == "pinecone-bundle/0"
    assert b["counts"]["tracks"] == 2
    by = {t["callsign"]: t for t in b["tracks"]}
    assert set(by) == {"ALPHA", "TRK1"}
    assert by["ALPHA"]["platform"] == "ATAK-CIV" and by["ALPHA"]["device"] == "SM-S911U1"
    assert by["ALPHA"]["n"] == 2 and by["TRK1"]["n"] == 1
    assert b["point_fields"][0] == "servertime_ms"
    assert by["ALPHA"]["points"] == sorted(by["ALPHA"]["points"], key=lambda p: p[0])
    assert len(by["ALPHA"]["points"]) == 2, "two reports stay two reports; nothing is filled in"


def test_it_stops_before_it_fills_the_disk(tmp_path: Path) -> None:
    archive = load("pinecone_archive")
    recorder = load("pinecone_recorder")
    a = archive.Archive(str(tmp_path / "a.db"))

    # A server that gains one report between reads and answers the cursor honestly. The stub used
    # to return `row(after + 1)` regardless, which stopped modelling a source once the recorder
    # began reading from below its high-water mark. Scaffolding only: every assertion below is
    # unchanged.
    server: list[dict[str, Any]] = []

    def source(after: int, limit: int) -> list[dict[str, Any]]:
        server.append(row(len(server) + 1, secs=len(server) * 10))
        return [r for r in server if r["id"] > after][:limit]

    plenty = recorder.poll_once(source, a, free_bytes=lambda: 50 * 1024**3)
    assert plenty["recorded"] == 1 and plenty["recording"] is True
    cramped = recorder.poll_once(source, a, free_bytes=lambda: 10 * 1024**2)
    assert cramped["recorded"] == 0
    assert cramped["recording"] is False
    assert "room" in cramped["reason"].lower() or "space" in cramped["reason"].lower()
    assert a.stats()["count"] == 1, "nothing was written while there was no room"
    back = recorder.poll_once(source, a, free_bytes=lambda: 50 * 1024**3)
    assert back["recorded"] == 1 and back["recording"] is True, "and it picks up again when there is"


# The four findings the pre-UAT review of this slice returned as gating, each with the criterion it
# belongs to. They were found by reading the diff and the live box, not by these tests, which is
# the point of the review; these keep them found.


def test_a_report_that_commits_out_of_order_is_still_recorded(tmp_path: Path) -> None:
    """Criterion 1, the half that was not proven: neither a gap nor a duplicate.

    The source table's id comes from a sequence, and a sequence value is handed out before the
    transaction commits, so a row with a lower id can appear after one already read. Reading from
    the high-water mark alone steps over it for good.
    """
    archive = load("pinecone_archive")
    recorder = load("pinecone_recorder")
    a = archive.Archive(str(tmp_path / "a.db"))

    visible: list[dict[str, Any]] = [row(1), row(3, secs=20)]  # id 2 is still uncommitted

    def source(after: int, limit: int) -> list[dict[str, Any]]:
        return [r for r in sorted(visible, key=lambda r: r["id"]) if r["id"] > after][:limit]

    first = recorder.poll_once(source, a, free_bytes=lambda: 10**12)
    assert first["recorded"] == 2
    assert a.cursor() == 3

    visible.append(row(2, secs=10))  # it commits now, below the mark already reached
    second = recorder.poll_once(source, a, free_bytes=lambda: 10**12)

    assert second["recorded"] == 1, "the late commit is picked up, not stepped over"
    assert sorted(r["id"] for r in a.window(0, 4 * 10**12)) == [1, 2, 3]
    assert recorder.poll_once(source, a, free_bytes=lambda: 10**12)["recorded"] == 0, "and not written twice"


def test_a_read_that_fails_is_said_so_not_reported_as_a_quiet_net(tmp_path: Path) -> None:
    """Criterion 5. A wrong password, a stopped database or a missing psql produced a recorder that
    said it was recording and recorded nothing, indefinitely and silently."""
    archive = load("pinecone_archive")
    recorder = load("pinecone_recorder")
    a = archive.Archive(str(tmp_path / "a.db"))

    def broken(after: int, limit: int) -> list[dict[str, Any]]:
        raise recorder.SourceError("psql exited 2 reading the server's table")

    out = recorder.poll_once(broken, a, free_bytes=lambda: 10**12)

    assert out["recording"] is False
    assert "psql exited 2" in out["reason"]
    assert a.get_meta("recording") == "no"
    assert "psql exited 2" in a.get_meta("reason")


def test_it_stamps_a_heartbeat_even_when_nothing_came_back(tmp_path: Path) -> None:
    """Criterion 5, 'when it last ran'. Without a per-pass stamp, a healthy recorder on a quiet net
    and one that died an hour ago are the same page."""
    archive = load("pinecone_archive")
    recorder = load("pinecone_recorder")
    a = archive.Archive(str(tmp_path / "a.db"))

    out = recorder.poll_once(lambda after, limit: [], a, free_bytes=lambda: 10**12)

    assert out["recorded"] == 0
    assert out["last_checked"], "the pass stamped itself"
    assert a.get_meta("last_checked") == out["last_checked"]
    assert a.stats()["last_checked"] == out["last_checked"]
    assert a.get_meta("last_run") == "", "and did not claim to have written anything"


def test_free_space_that_cannot_be_read_is_not_a_full_disk(tmp_path: Path) -> None:
    """Criterion 5. A failed statvfs returned 0, which read to the operator as a full disk and
    stopped the record."""
    archive = load("pinecone_archive")
    recorder = load("pinecone_recorder")
    a = archive.Archive(str(tmp_path / "a.db"))

    out = recorder.poll_once(lambda after, limit: [row(1)], a, free_bytes=lambda: -1)

    assert out["recording"] is True, "it keeps the record rather than stopping on an unknown"
    assert out["recorded"] == 1
    assert "could not be read" in out["reason"]


# The three findings the re-review returned. Two of them were introduced by the fixes for the
# first round, which is the argument for reviewing a fix as hard as the thing it fixed.


def test_a_pass_that_writes_nothing_does_not_claim_to_have_written(tmp_path: Path) -> None:
    """Criterion 5. Reading a band below the mark means the rows are never empty once anything is
    held, so stamping the write time on rows read rather than rows written made a dead-quiet net
    tick over every ten seconds as though it were busy."""
    archive = load("pinecone_archive")
    recorder = load("pinecone_recorder")
    a = archive.Archive(str(tmp_path / "a.db"))
    held = [row(i, secs=i * 10) for i in range(1, 11)]

    def source(after: int, limit: int) -> list[dict[str, Any]]:
        return [r for r in held if r["id"] > after][:limit]

    first = recorder.poll_once(source, a, free_bytes=lambda: 10**12)
    assert first["recorded"] == 10
    wrote = a.get_meta("last_run")
    assert wrote

    quiet = recorder.poll_once(source, a, free_bytes=lambda: 10**12)

    assert quiet["read"] == 10, "the band below the mark is re-read, so rows did come back"
    assert quiet["recorded"] == 0, "and none of them were new"
    assert a.get_meta("last_run") == wrote, "so the write time did not move"
    assert a.get_meta("last_checked") != "", "the heartbeat still ticks, which is the honest signal"


def test_a_first_run_starts_where_the_server_has_got_to(tmp_path: Path) -> None:
    """Starting at the server's current position, which was the default until it was reversed on
    4 September 2026 and is now what `--no-backfill` asks for.

    The assertions are exactly as they were committed; the calls say which behaviour they want,
    because the default they used to rely on has changed. Spec 004 covers the new default."""
    archive = load("pinecone_archive")
    recorder = load("pinecone_recorder")
    a = archive.Archive(str(tmp_path / "a.db"))
    history = [row(i, secs=i) for i in range(1, 3001)]

    def source(after: int, limit: int) -> list[dict[str, Any]]:
        return [r for r in history if r["id"] > after][:limit]

    started = recorder.seed_if_empty(a, lambda: 3000, backfill=False)

    assert started == 3000
    assert a.cursor() == 3000, "it picks up from the server's position, not from id 0"
    out = recorder.poll_once(source, a, free_bytes=lambda: 10**12, backfill=False)
    assert out["recorded"] == 0, "nothing from before the install is dragged in"
    history.append(row(3001, secs=3001))
    assert (
        recorder.poll_once(source, a, free_bytes=lambda: 10**12, backfill=False)["recorded"] == 1
    ), "and it records from now on"


def test_the_floor_holds_across_a_restart_and_the_lag_cannot_reach_below_it(tmp_path: Path) -> None:
    archive = load("pinecone_archive")
    recorder = load("pinecone_recorder")
    path = str(tmp_path / "a.db")
    a = archive.Archive(path)
    recorder.seed_if_empty(a, lambda: 3000, backfill=False)
    assert a.read_from(recorder.CURSOR_LAG) == 3000, "the lag band stops at the floor"
    a.close()

    b = archive.Archive(path)
    assert b.floor() == 3000
    assert recorder.seed_if_empty(b, lambda: 9999, backfill=False) is None, "a record already going is not re-seeded"
    assert b.cursor() == 3000


def test_a_batch_that_does_not_outrun_the_lag_is_refused(tmp_path: Path) -> None:
    """Otherwise every pass refills itself with the band it just re-read, the cursor never moves,
    and the page goes on saying it is recording."""
    import pytest

    archive = load("pinecone_archive")
    recorder = load("pinecone_recorder")
    a = archive.Archive(str(tmp_path / "a.db"))
    with pytest.raises(ValueError, match="larger than the cursor lag"):
        recorder.poll_once(lambda after, limit: [], a, free_bytes=lambda: 10**12, batch=recorder.CURSOR_LAG)


def test_a_window_that_was_cut_says_so(tmp_path: Path) -> None:
    """A silent cut is worse than a refusal: the counts in the bundle would otherwise certify a
    complete read of a window that was not complete."""
    archive = load("pinecone_archive")
    a = archive.Archive(str(tmp_path / "a.db"))
    a.record([row(i, secs=i) for i in range(1, 51)])

    whole = a.bundle(0, 4 * 10**12)
    assert "truncated" not in whole, "an uncut window says nothing about cutting"

    import unittest.mock

    with unittest.mock.patch.object(archive, "MAX_WINDOW_ROWS", 10):
        cut = a.bundle(0, 4 * 10**12, source="test")

    assert cut["counts"]["rows_read"] == 10
    assert cut["truncated"] == {"held": 50, "returned": 10, "cap": 10}
    assert cut["counts"]["rows_capped"] == 40


# The third review pass. Both of these live at a seam between two earlier fixes, which is where
# every finding of this round was: the fix for one round routing around the fix from the last.


def test_the_floor_survives_the_archive_being_recreated_underneath(tmp_path: Path) -> None:
    """`reopen_if_gone` exists so a deleted archive does not silently stop the record. It creates an
    empty database with no meta, so the floor went with it, and the next pass read from id 0: the
    whole retained table again, at full rate, without anyone restarting anything."""
    archive = load("pinecone_archive")
    recorder = load("pinecone_recorder")
    path = tmp_path / "a.db"
    a = archive.Archive(str(path))
    history = [row(i, secs=i) for i in range(1, 3001)]
    asked_from: list[int] = []

    def source(after: int, limit: int) -> list[dict[str, Any]]:
        asked_from.append(after)
        return [r for r in history if r["id"] > after][:limit]

    first = recorder.poll_once(source, a, free_bytes=lambda: 10**12, head_id=lambda: 3000, backfill=False)
    assert first["seeded"] == 3000
    assert asked_from == [3000]

    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    after_loss = recorder.poll_once(source, a, free_bytes=lambda: 10**12, head_id=lambda: 3000, backfill=False)

    assert asked_from[-1] == 3000, "it re-seeds rather than reading the table from the beginning"
    assert after_loss["seeded"] == 3000, "and says that it started again"
    assert after_loss["recorded"] == 0


def test_a_first_run_that_cannot_reach_the_server_says_so_and_tries_again(tmp_path: Path) -> None:
    """The read inside the loop was made to report its failure. The read that happens before the
    loop, asking where the server's table has got to, was not, so a first run against a database
    that was not answering left a traceback in the journal and nothing on the page. On a cold start
    Pinecone comes up before TAK Server is ready, so this is the ordinary case, not a rare one."""
    archive = load("pinecone_archive")
    recorder = load("pinecone_recorder")
    a = archive.Archive(str(tmp_path / "a.db"))

    def unreachable() -> int:
        raise recorder.SourceError("psql exited 2 reading where the server's table has got to")

    out = recorder.poll_once(lambda after, limit: [], a, free_bytes=lambda: 10**12, head_id=unreachable)

    assert out["recording"] is False
    assert "psql exited 2" in out["reason"]
    assert a.get_meta("reason") == out["reason"], "the reason reaches the record, and so the page"
    assert a.get_meta("last_checked"), "and the pass still counts as a check"
    assert a.get_meta("cursor_floor") == "", "nothing was seeded from a failed read"

    later = recorder.poll_once(
        lambda after, limit: [], a, free_bytes=lambda: 10**12, head_id=lambda: 4242, backfill=False
    )

    assert later["seeded"] == 4242, "and it seeds on the next pass once the server answers"
    assert later["recording"] is True
    assert a.get_meta("reason") == ""


def test_a_pass_that_seeds_and_then_fails_its_read_still_says_where_it_started(tmp_path: Path) -> None:
    """The floor is set by the seed and the read fails immediately after, so the one pass that
    could ever report where the record started is also the pass that failed. Reporting None there
    lost that line for the life of the box, because every later pass short-circuits on the floor."""
    archive = load("pinecone_archive")
    recorder = load("pinecone_recorder")
    a = archive.Archive(str(tmp_path / "a.db"))

    def read_fails(after: int, limit: int) -> list[dict[str, Any]]:
        raise recorder.SourceError("psql exited 2 reading the server's table")

    out = recorder.poll_once(read_fails, a, free_bytes=lambda: 10**12, head_id=lambda: 777, backfill=False)

    assert out["seeded"] == 777, "the seed happened and is reported, even though the read then failed"
    assert a.floor() == 777, "and the floor really was set"
    assert out["recording"] is False and "psql exited 2" in out["reason"]


# The seventh review pass. Both of these were always here: they were missed because four
# consecutive passes were looking at the installer, not the recorder.


def test_a_pass_that_fails_for_any_other_reason_still_reaches_the_page(tmp_path: Path) -> None:
    """Criterion 5. Only a source failure was reported. Anything else unwound past the reporting
    into the retry, so the page went on saying "recording" with a heartbeat that kept moving,
    which is the exact picture criterion 5 was written to prevent."""
    archive = load("pinecone_archive")
    recorder = load("pinecone_recorder")
    a = archive.Archive(str(tmp_path / "a.db"))
    a.set_meta("recording", "yes")
    a.set_meta("reason", "")

    class TheArchiveWouldNotAnswer(Exception):
        pass

    reason = recorder.report_failure(a, TheArchiveWouldNotAnswer("disk I/O error"))

    assert a.get_meta("recording") == "no"
    assert a.get_meta("reason") == reason
    assert "the last pass failed" in reason
    assert "TheArchiveWouldNotAnswer" in reason, "and says what kind of failure it was"
    assert "disk I/O error" in reason


def test_reporting_a_failure_never_raises_even_when_the_archive_is_what_failed(tmp_path: Path) -> None:
    """The one thing this function must not do is fail, because it runs on the path where something
    has already failed and the archive itself is a candidate."""
    recorder = load("pinecone_recorder")

    class Broken:
        def set_meta(self, key: str, value: str) -> None:
            raise OSError("the archive is gone")

    reason = recorder.report_failure(Broken(), RuntimeError("something else"))

    assert "the last pass failed" in reason


def test_a_report_too_long_for_the_default_field_limit_is_still_read(tmp_path: Path) -> None:
    """A CoT detail blob can be long and the default limit is 128 KB. Over it, the reader raised an
    error that was neither a source failure nor a programmer error, so the batch holding that row
    was retried for ever, the cursor never moved, and the page reported health throughout."""
    import csv
    import io

    recorder = load("pinecone_recorder")
    long_detail = "<detail><remarks>" + ("x" * 200_000) + "</remarks></detail>"
    text = 'id,uid,detail\n1,U,"' + long_detail + '"\n'

    rows = list(csv.DictReader(io.StringIO(text)))

    assert len(rows) == 1
    assert len(rows[0]["detail"]) > 128 * 1024, "the limit the module raises is the one in force"
    assert recorder.csv.field_size_limit() >= 1 << 26
