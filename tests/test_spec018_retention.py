"""Spec 018: the archive stops growing for ever.

The archive is the movements of identifiable people, and from the release that added the
connection log it also records when a named person's handset was on the net. Until now it grew
without limit and nothing deleted anything, which threat note 003 named as unsolved. That is the
gate on 1.0.0: an issued version is a promise, and "it grows for ever" is not a promise to make
about personal data.

A default install now keeps reports and messages for 365 days and connection events for 90. Both
are configurable, keeping for ever is still available by asking for it, and the status page says
what the policy is so it is visible rather than buried in a file.

Every row here is synthetic.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from conftest import load

DAY_MS = 86_400_000


def stamp(ms: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ms / 1000)) + "+00"


def report(i: int, at_ms: int) -> dict[str, Any]:
    return {
        "id": i,
        "uid": "ANDROID-1",
        "cot_type": "a-f-G-U-C",
        "how": "m-g",
        "start": stamp(at_ms),
        "time": stamp(at_ms),
        "stale": stamp(at_ms),
        "servertime": stamp(at_ms),
        "lat": 51.2,
        "lon": -1.5,
        "point_hae": 95.0,
        "point_ce": 9.0,
        "point_le": 9.0,
        "detail": '<detail><contact callsign="ALPHA"/></detail>',
        "groups": "MilUX",
    }


def connection(i: int, at_ms: int) -> dict[str, Any]:
    return {
        "id": i,
        "servertime": stamp(at_ms),
        "event": "Connected",
        "callsign": "ALPHA",
        "uid": "UID-ALPHA",
        "username": "alpha",
        "team": "Cyan",
        "role": "Team Member",
        "client_version": "5.8.0",
        "groups": "MilUX",
    }


def archive_with_history(tmp_path: Path, now_ms: int) -> Any:
    archive = load("pinecone_archive")
    a = archive.Archive(str(tmp_path / "a.db"))
    a.record([report(1, now_ms - 400 * DAY_MS), report(2, now_ms - 100 * DAY_MS), report(3, now_ms)], "report")
    a.record([report(11, now_ms - 400 * DAY_MS), report(12, now_ms)], "chat")
    a.record(
        [connection(1, now_ms - 200 * DAY_MS), connection(2, now_ms - 30 * DAY_MS), connection(3, now_ms)],
        "connection",
    )
    return a


NOW = 1_789_000_000_000


def test_the_default_policy_keeps_a_year_of_reports_and_a_quarter_of_connections(tmp_path: Path) -> None:
    """Criterion 1. Deletion actually happens, and the two classes are kept for different lengths
    because a presence and absence record for a named person is the more sensitive of the two and
    the least useful once it is old."""
    a = archive_with_history(tmp_path, NOW)
    archive = load("pinecone_archive")

    removed = a.prune(archive.DEFAULT_RETENTION, now_ms=NOW)

    assert removed == {"report": 1, "chat": 1, "connection": 1}
    assert a.count("report") == 2, "the 400-day-old report survived a 365-day policy"
    assert a.count("chat") == 1
    assert a.count("connection") == 2, "the 200-day-old connection survived a 90-day policy"


def test_keeping_for_ever_is_still_available_by_asking(tmp_path: Path) -> None:
    """Criterion 2. Zero means keep everything. An operator who wants the whole history says so,
    and nothing is deleted behind them."""
    a = archive_with_history(tmp_path, NOW)

    removed = a.prune({"report": 0, "chat": 0, "connection": 0}, now_ms=NOW)

    assert removed == {"report": 0, "chat": 0, "connection": 0}
    assert a.count("report") == 3 and a.count("chat") == 2 and a.count("connection") == 3


def test_pruning_does_not_move_the_cursor_or_reopen_the_backfill(tmp_path: Path) -> None:
    """Criterion 3, and the one that would quietly destroy the record if it were wrong.

    The cursor is the highest id held. Deleting the oldest rows must not lower it, or the recorder
    would read the server's history again from the beginning; and an archive pruned back to empty
    must not look like a fresh install to the seeding step, or it would set a new floor and drop
    everything before it.
    """
    a = archive_with_history(tmp_path, NOW)
    before = a.cursor("report")

    a.prune({"report": 1, "chat": 1, "connection": 1}, now_ms=NOW + DAY_MS * 2)

    assert a.count("report") == 0, "the fixture did not actually empty"
    assert a.cursor("report") == before, "pruning moved the cursor back"
    recorder = load("pinecone_recorder")
    assert (
        recorder.seed_if_empty(a, lambda: 99_999, table="report") is None
    ), "a pruned archive was treated as a fresh install and would have re-seeded"


def test_a_policy_is_read_from_the_environment_and_falls_back_to_the_default() -> None:
    """Criterion 4. The policy lives beside the credential in the environment file, which is the
    file install.sh carries forward across an update, so a choice is not silently reverted."""
    recorder = load("pinecone_recorder")
    archive = load("pinecone_archive")

    assert recorder.retention_policy({}) == archive.DEFAULT_RETENTION
    chosen = recorder.retention_policy({"PINECONE_KEEP_DAYS": "30", "PINECONE_KEEP_CONNECTION_DAYS": "7"})
    assert chosen == {"report": 30, "chat": 30, "connection": 7}
    assert recorder.retention_policy({"PINECONE_KEEP_DAYS": "0"})["report"] == 0, "0 means keep for ever"
    assert (
        recorder.retention_policy({"PINECONE_KEEP_DAYS": "nonsense"}) == archive.DEFAULT_RETENTION
    ), "an unreadable value falls back to the default rather than deleting everything"
    assert (
        recorder.retention_policy({"PINECONE_KEEP_DAYS": "-5"}) == archive.DEFAULT_RETENTION
    ), "a negative value is not a licence to delete the archive"


def test_the_status_page_says_what_the_policy_is(tmp_path: Path) -> None:
    """Criterion 5. Visible rather than buried in a file. An operator should not have to read the
    environment file to find out what is being deleted and when."""
    archive = load("pinecone_archive")
    a = archive.Archive(str(tmp_path / "a.db"))
    a.record([report(1, NOW)], "report")
    a.set_meta("retention", "report=365,chat=365,connection=90")
    a.set_meta("last_pruned", "2026-09-13 09:00:00.000000+00")

    stats = a.stats()

    assert stats["retention"] == "report=365,chat=365,connection=90"
    assert stats["last_pruned"] == "2026-09-13 09:00:00.000000+00"


def test_a_prune_records_when_it_last_ran(tmp_path: Path) -> None:
    """Criterion 6. A retention policy nobody can see running is a claim, not a control."""
    a = archive_with_history(tmp_path, NOW)
    archive = load("pinecone_archive")
    assert a.get_meta("last_pruned") == ""

    a.prune(archive.DEFAULT_RETENTION, now_ms=NOW)

    assert a.get_meta("last_pruned"), "nothing recorded that the policy had run"
    assert a.get_meta("retention") == "report=365,chat=365,connection=90"
