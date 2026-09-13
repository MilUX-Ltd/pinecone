"""Spec 019: the two seams 1.0.0 promises not to break.

An issued 1.0.0 makes every later break a MAJOR, so before the number goes out it has to be clear
what is being promised. Two things a consumer can build on: the bundle a window exports as, which
opens anywhere with no server, and the archive on a box, which every future release has to be able
to read and upgrade in place.

These tests are the promise in executable form. They will look pedantic until the day one of them
fails, which is the day somebody would otherwise have shipped a silent break to a box in the field.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from conftest import load

# pinecone-bundle/0. Adding a key is a MINOR; removing or repurposing one is a MAJOR.
BUNDLE_KEYS = {"format", "source", "window", "counts", "point_fields", "tracks"}
TRACK_KEYS = {
    "uid",
    "callsign",
    "platform",
    "device",
    "os",
    "version",
    "team",
    "role",
    "type",
    "n",
    "first",
    "last",
    "median_interval_ms",
    "points",
}
# The order is the contract, not just the membership: a point is a list, read by position.
POINT_FIELDS = [
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
]

# The archive's shape. A column may be added; one that exists must keep its name and meaning.
ARCHIVE_TABLES = {"report", "chat", "connection", "meta"}
REPORT_COLUMNS = {
    "id",
    "uid",
    "cot_type",
    "how",
    "device_time",
    "device_start",
    "stale",
    "servertime",
    "arrived",
    "lat",
    "lon",
    "hae",
    "ce",
    "le",
    "detail",
    "groups",
}
CONNECTION_COLUMNS = {
    "id",
    "servertime",
    "arrived",
    "event",
    "callsign",
    "uid",
    "username",
    "team",
    "role",
    "client_version",
    "groups",
}


def test_the_bundle_format_is_what_it_says_it_is() -> None:
    """The seam the player is written against, and the thing an exported window has to open with
    on a machine that has never heard of Pinecone."""
    bundle = json.loads((Path(__file__).resolve().parent.parent / "data" / "synthetic.json").read_text())

    assert bundle["format"] == "pinecone-bundle/0"
    assert set(bundle) >= BUNDLE_KEYS, f"missing from the bundle: {BUNDLE_KEYS - set(bundle)}"
    assert bundle["point_fields"] == POINT_FIELDS, "a point is read by position, so the order is the contract"
    assert set(bundle["window"]) == {"start", "end"}, "a window is start and end, in epoch milliseconds"
    track = bundle["tracks"][0]
    assert set(track) >= TRACK_KEYS, f"missing from a track: {TRACK_KEYS - set(track)}"
    assert len(track["points"][0]) == len(POINT_FIELDS), "a point row must carry exactly the declared fields"


def test_the_archive_shape_is_what_a_later_release_must_read(tmp_path: Path) -> None:
    """Every box in the field carries one of these. A release that renames a column here does not
    fail loudly; it fails on somebody's box, months later, with their exercise in it."""
    archive = load("pinecone_archive")
    a = archive.Archive(str(tmp_path / "a.db"))

    names = {r["name"] for r in a.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert names >= ARCHIVE_TABLES, f"missing tables: {ARCHIVE_TABLES - names}"
    report = {r["name"] for r in a.db.execute("PRAGMA table_info(report)")}
    assert report == REPORT_COLUMNS, f"the report table's shape moved: {report ^ REPORT_COLUMNS}"
    chat = {r["name"] for r in a.db.execute("PRAGMA table_info(chat)")}
    assert chat == REPORT_COLUMNS, "chat is report-shaped, so a window can union the two"
    conn = {r["name"] for r in a.db.execute("PRAGMA table_info(connection)")}
    assert conn == CONNECTION_COLUMNS, f"the connection table's shape moved: {conn ^ CONNECTION_COLUMNS}"


def test_the_archive_declares_its_own_version(tmp_path: Path) -> None:
    """So a future release can tell what it is looking at rather than inferring it from columns.

    Written into the file itself with PRAGMA user_version, which SQLite carries for exactly this
    and which costs nothing to read.
    """
    archive = load("pinecone_archive")
    a = archive.Archive(str(tmp_path / "a.db"))

    assert a.db.execute("PRAGMA user_version").fetchone()[0] == archive.SCHEMA_VERSION
    assert archive.SCHEMA_VERSION >= 1


def test_an_archive_from_before_the_version_existed_is_upgraded_not_refused(tmp_path: Path) -> None:
    """The promise that matters most, because it is the one with somebody's data behind it. An
    archive written before any of this must open, gain what it is missing, and keep its rows."""
    path = tmp_path / "old.db"
    old = sqlite3.connect(str(path))
    old.executescript(
        "CREATE TABLE report (id INTEGER PRIMARY KEY, uid TEXT NOT NULL, cot_type TEXT, how TEXT,"
        " device_time TEXT, device_start TEXT, stale TEXT, servertime TEXT NOT NULL, arrived TEXT NOT NULL,"
        " lat REAL, lon REAL, hae REAL, ce REAL, le REAL, detail TEXT);"
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);"
        "INSERT INTO report (id, uid, servertime, arrived)"
        " VALUES (1, 'OLD-1', '2026-09-13 08:00:00+00', '2026-09-13 08:00:01+00');"
    )
    old.commit()
    old.close()

    archive = load("pinecone_archive")
    a = archive.Archive(str(path))

    assert a.count("report") == 1, "the row that was already there did not survive the upgrade"
    assert {r["name"] for r in a.db.execute("PRAGMA table_info(report)")} == REPORT_COLUMNS
    assert a.db.execute("PRAGMA user_version").fetchone()[0] == archive.SCHEMA_VERSION


def test_the_environment_file_keys_are_the_ones_documented() -> None:
    """The third seam, and the one an operator actually edits. install.sh rewrites this file
    wholesale on every run, so a key it stops writing is a setting an update silently deletes."""
    root = Path(__file__).resolve().parent.parent
    installer = (root / "install.sh").read_text()
    for key in (
        "PGHOST",
        "PGPORT",
        "PGDATABASE",
        "PGUSER",
        "PGPASSWORD",
        "PINECONE_BIND",
        "PINECONE_PORT",
        "PINECONE_BACKFILL",
        "PINECONE_CHAT",
        "PINECONE_RECORD",
        "PINECONE_KEEP_DAYS",
        "PINECONE_KEEP_CONNECTION_DAYS",
    ):
        assert key in installer, f"{key} is documented as a setting but the installer never writes it"
    contracts = (root / "CONTRACTS.md").read_text()
    for key in ("PINECONE_KEEP_DAYS", "PINECONE_KEEP_CONNECTION_DAYS", "pinecone-bundle/0"):
        assert key in contracts, f"{key} is a promise the contract document does not make"


def _unused(_: Any) -> None:
    return None
