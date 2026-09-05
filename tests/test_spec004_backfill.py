"""Spec 004, criteria 1 to 3: the recorder takes the history the server still holds, paced, and
survives being stopped part-way through.

Decided 4 September 2026: it should pull history from the server table. Every report here is
synthetic; no real recording goes anywhere near a test.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from conftest import load

DETAIL = '<detail><contact callsign="ALPHA"/><takv platform="ATAK-CIV"/></detail>'


def row(i: int, secs: int = 0) -> dict[str, Any]:
    t = f"2026-08-01 09:{secs // 60:02d}:{secs % 60:02d}+00"
    return {
        "id": i,
        "uid": "ANDROID-1",
        "cot_type": "a-f-G-U-C",
        "how": "m-g",
        "start": t,
        "time": t,
        "stale": t,
        "servertime": t,
        "lat": 51.2 + i * 1e-5,
        "lon": -1.5,
        "point_hae": 90.0,
        "point_ce": 9.0,
        "point_le": 9.0,
        "detail": DETAIL,
    }


def server_holding(count: int) -> tuple[list[dict[str, Any]], list[int]]:
    """A table with `count` reports already in it, and a log of where each read started."""
    held = [row(i, secs=i) for i in range(1, count + 1)]
    asked_from: list[int] = []

    def source(after: int, limit: int) -> list[dict[str, Any]]:
        asked_from.append(after)
        return [r for r in held if r["id"] > after][:limit]

    source.held = held  # type: ignore[attr-defined]
    return source, asked_from  # type: ignore[return-value]


def test_a_first_run_takes_the_history_that_is_there(tmp_path: Path) -> None:
    """Criterion 1. The whole point of the change: an estate that has been running for months has
    exercises in its table that a debrief should be able to reach."""
    archive = load("pinecone_archive")
    recorder = load("pinecone_recorder")
    a = archive.Archive(str(tmp_path / "a.db"))
    source, _ = server_holding(5000)

    recorder.catch_up(source, a, head_id=lambda: 5000, pause=lambda _s: None)

    assert a.stats()["count"] == 5000, "everything the server held is in the record"
    assert a.cursor() == 5000


def test_it_records_the_oldest_first_so_a_partial_backfill_is_a_prefix_not_a_scatter(
    tmp_path: Path,
) -> None:
    """Criterion 1. A backfill that is interrupted must leave a usable record of the beginning,
    not holes throughout."""
    archive = load("pinecone_archive")
    recorder = load("pinecone_recorder")
    a = archive.Archive(str(tmp_path / "a.db"))
    source, _ = server_holding(5000)

    recorder.poll_once(source, a, free_bytes=lambda: 10**12, head_id=lambda: 5000)

    ids = sorted(r["id"] for r in a.window(0, 4 * 10**12))
    # `ids == list(range(1, len(ids) + 1))` is true of an empty list, so on the code this test was
    # written against it passed by recording nothing at all. Say the floor out loud.
    assert len(ids) >= 1000, "it took a real batch, not nothing"
    assert ids[0] == 1, "starting at the oldest report the server holds"
    assert ids == list(range(1, len(ids) + 1)), "an unbroken run, not a scatter"
    assert len(ids) < 5000, "and it did not take the lot in one pass"


def test_it_pauses_between_catch_up_batches(tmp_path: Path) -> None:
    """Criterion 2. The objection to backfilling was a sustained hot loop against the database
    Pinecone is meant to sit lightly beside. The answer is pacing, not refusing."""
    archive = load("pinecone_archive")
    recorder = load("pinecone_recorder")
    a = archive.Archive(str(tmp_path / "a.db"))
    source, _ = server_holding(5000)
    pauses: list[float] = []

    recorder.catch_up(source, a, head_id=lambda: 5000, pause=pauses.append)

    assert len(pauses) >= 2, "it paused between batches, not only at the end"
    assert all(p > 0 for p in pauses), "and the pause is a real one"


def test_it_can_be_told_not_to_backfill(tmp_path: Path) -> None:
    """Criterion 2. The old behaviour stays available for an operator who wants it."""
    archive = load("pinecone_archive")
    recorder = load("pinecone_recorder")
    a = archive.Archive(str(tmp_path / "a.db"))
    source, asked_from = server_holding(5000)

    recorder.poll_once(source, a, free_bytes=lambda: 10**12, head_id=lambda: 5000, backfill=False)

    assert a.stats()["count"] == 0, "nothing from before the install"
    assert a.cursor() == 5000, "and it starts from where the server has got to"
    assert asked_from == [5000], "it never asked for the history at all"


def test_an_interrupted_backfill_resumes_where_it_stopped(tmp_path: Path) -> None:
    """Criterion 3."""
    archive = load("pinecone_archive")
    recorder = load("pinecone_recorder")
    path = str(tmp_path / "a.db")
    a = archive.Archive(path)
    source, _ = server_holding(5000)

    recorder.poll_once(source, a, free_bytes=lambda: 10**12, head_id=lambda: 5000)
    got_first = a.stats()["count"]
    a.close()

    b = archive.Archive(path)
    recorder.catch_up(source, b, head_id=lambda: 5000, pause=lambda _s: None)

    assert got_first > 0
    assert b.stats()["count"] == 5000, "no gap"
    ids = sorted(r["id"] for r in b.window(0, 4 * 10**12))
    assert len(set(ids)) == len(ids), "and no duplicate"
