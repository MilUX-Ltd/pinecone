#!/usr/bin/env python3
"""Pinecone's own record: an append-only archive of what the TAK Server routed.

The whole argument for the product (D5). TAK's retention service can prune whatever it likes and
this file still holds the record. One SQLite file, standard library only (ADR-002), written by the
recorder and read for a window as the bundle the player already understands.

Nothing here interprets a report. The `detail` blob is stored exactly as it arrived (D9) and every
timestamp is kept (D10): the device's own `time` and `start`, the server's receipt time, and the
moment Pinecone first saw it.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS report (
    id           INTEGER PRIMARY KEY,   -- the source table's own id: the cursor and the identity
    uid          TEXT NOT NULL,
    cot_type     TEXT,
    how          TEXT,
    device_time  TEXT,
    device_start TEXT,
    stale        TEXT,
    servertime   TEXT NOT NULL,
    arrived      TEXT NOT NULL,
    lat          REAL,
    lon          REAL,
    hae          REAL,
    ce           REAL,
    le           REAL,
    detail       TEXT
);
CREATE INDEX IF NOT EXISTS report_servertime ON report (servertime);
CREATE INDEX IF NOT EXISTS report_uid_servertime ON report (uid, servertime);
-- GeoChat, from the server's own chat table (cot_router_chat on TAK Server 5.8), which has the
-- same columns and its own id sequence. Its own table here, because the ids collide with the
-- reports' and the id is the cursor; a window reads both, merged.
CREATE TABLE IF NOT EXISTS chat (
    id           INTEGER PRIMARY KEY,
    uid          TEXT NOT NULL,
    cot_type     TEXT,
    how          TEXT,
    device_time  TEXT,
    device_start TEXT,
    stale        TEXT,
    servertime   TEXT NOT NULL,
    arrived      TEXT NOT NULL,
    lat          REAL,
    lon          REAL,
    hae          REAL,
    ce           REAL,
    le           REAL,
    detail       TEXT
);
CREATE INDEX IF NOT EXISTS chat_servertime ON chat (servertime);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

TABLES = ("report", "chat")


def meta_key(table: str, name: str) -> str:
    """The report table's keys are the ones the record has always had; the chat table's are
    prefixed, so an archive written before chat was recorded reads exactly as it did."""
    if table not in TABLES:
        raise ValueError(f"no such table in the archive: {table!r}")
    return name if table == "report" else f"{table}_{name}"


MAX_WINDOW_ROWS = 250_000


def _utcnow() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f+00")


class Archive:
    """The record. Opened by the recorder to write and by the server to read; both may hold it."""

    def __init__(self, path: str, read_only: bool = False) -> None:
        self.path = path
        self.read_only = read_only
        if read_only:
            # A reader is a reader. Opening the recorder's file read-write to serve a page made the
            # page a writer to the record, contended for the write lock, and would fail outright the
            # moment the server is given a read-only view of it.
            self.db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30, isolation_level=None)
            self.db.row_factory = sqlite3.Row
            return
        self._open()

    def _open(self) -> None:
        parent = os.path.dirname(os.path.abspath(self.path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        existed = os.path.exists(self.path)
        self.db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        if not existed:
            # The record is the movements of identifiable people, so the database file is not
            # world-readable whatever umask this process carries. Its -wal and -shm siblings hold
            # real reports too and take their mode from the umask, which is why both units set
            # UMask=0027 and the installer makes the directory 0750. Run by hand off the box, the
            # sidecars are only as private as the shell that made them.
            with contextlib.suppress(OSError):
                os.chmod(self.path, 0o640)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # ---------- writing ----------
    def reopen_if_gone(self) -> bool:
        """If the file has been deleted underneath us, open a new one rather than writing on into
        an unlinked inode while the page reports no archive at all. True if it was reopened."""
        if self.read_only or os.path.exists(self.path):
            return False
        with contextlib.suppress(sqlite3.Error):
            self.db.close()
        self._open()
        return True

    def record(self, rows: list[dict[str, Any]], table: str = "report") -> int:
        """Write reports (or messages) that are not already held. Returns how many were new."""
        meta_key(table, "")  # refuses a table the archive does not have
        self.reopen_if_gone()
        if not rows:
            return 0
        seen = _utcnow()
        values = [
            (
                int(r["id"]),
                str(r["uid"]),
                r.get("cot_type"),
                r.get("how"),
                _text(r.get("time")),
                _text(r.get("start")),
                _text(r.get("stale")),
                _text(r.get("servertime")),
                seen,
                _num(r.get("lat")),
                _num(r.get("lon")),
                _num(r.get("point_hae")),
                _num(r.get("point_ce")),
                _num(r.get("point_le")),
                r.get("detail") or "",
            )
            for r in rows
        ]
        before = self.db.total_changes
        self.db.executemany(
            f"INSERT OR IGNORE INTO {table} (id, uid, cot_type, how, device_time, device_start, stale,"  # noqa: S608 - one of two literals
            " servertime, arrived, lat, lon, hae, ce, le, detail)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            values,
        )
        written = self.db.total_changes - before
        if written:
            # Only when something was actually written. The recorder re-reads a band below its own
            # mark every pass, so `rows` is never empty once the archive holds anything, and
            # stamping on rows rather than on writes made a dead-quiet net look like a busy one.
            self.set_meta("last_run", seen)
        return written

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def get_meta(self, key: str, default: str = "") -> str:
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else default

    @staticmethod
    def key(table: str, name: str) -> str:
        return meta_key(table, name)

    def count(self, table: str = "report") -> int:
        meta_key(table, "")
        row = self.db.execute(f"SELECT count(*) AS n FROM {table}").fetchone()  # noqa: S608 - one of two literals
        return int(row["n"] or 0)

    def cursor(self, table: str = "report") -> int:
        """The highest source id held, never below the floor. Where the recorder picks up."""
        meta_key(table, "")
        row = self.db.execute(f"SELECT max(id) AS m FROM {table}").fetchone()  # noqa: S608 - one of two literals
        held = int(row["m"]) if row and row["m"] is not None else 0
        return max(held, self.floor(table))

    def floor(self, table: str = "report") -> int:
        """The id the record starts at. Set once, on the first run, to whatever the server's table
        had reached at that moment: the archive starts when it starts, and history from before the
        install is not dragged in behind it."""
        try:
            return int(self.get_meta(meta_key(table, "cursor_floor")) or 0)
        except ValueError:
            return 0

    def set_floor(self, value: int, table: str = "report") -> None:
        self.set_meta(meta_key(table, "cursor_floor"), str(int(value)))

    def read_from(self, lag: int, table: str = "report") -> int:
        """Where the next pass starts reading: below the mark by the lag, but never below the
        floor, so the lag band cannot reach back into history the record deliberately excludes."""
        return max(self.floor(table), self.cursor(table) - max(0, int(lag)))

    # ---------- reading ----------
    def stats(self) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT count(*) AS n, min(servertime) AS first, max(servertime) AS last FROM report"
        ).fetchone()
        # In WAL mode most of a recent write is in the -wal file, so a size that counted only the
        # main file would tell an operator the record was empty when it is not.
        # The -wal file holds recent writes and is part of the record; -shm is a fixed scratch
        # mapping and is not, so counting it overstates a small archive by 32 KB.
        size = 0
        for suffix in ("", "-wal"):
            with contextlib.suppress(OSError):
                size += os.path.getsize(self.path + suffix)
        return {
            "count": int(row["n"] or 0),
            "first": row["first"],
            "last": row["last"],
            "bytes": size,
            "messages": self.count("chat"),
            "last_run": self.get_meta("last_run"),
            "last_checked": self.get_meta("last_checked"),
            "cursor": self.cursor(),
            "chat_cursor": self.cursor("chat"),
        }

    def count_window(self, start_ms: int, end_ms: int) -> int:
        bounds = (_ms_to_text(start_ms), _ms_to_text(end_ms, up=True))
        row = self.db.execute(
            "SELECT (SELECT count(*) FROM report WHERE servertime >= ? AND servertime < ?)"
            " + (SELECT count(*) FROM chat WHERE servertime >= ? AND servertime < ?) AS n",
            bounds + bounds,
        ).fetchone()
        return int(row["n"] or 0)

    def window(self, start_ms: int, end_ms: int, limit: int | None = None) -> list[dict[str, Any]]:
        """Every report received in a window, in the order the server received them.

        Capped: a day of a busy exercise is built in memory and serialised in one go inside the
        request thread, so an uncapped window is a way to take the page down from a browser.
        """
        cap = MAX_WINDOW_ROWS if limit is None else int(limit)
        bounds = (_ms_to_text(start_ms), _ms_to_text(end_ms, up=True))
        rows = self.db.execute(
            "SELECT * FROM (SELECT * FROM report WHERE servertime >= ? AND servertime < ?"
            " UNION ALL SELECT * FROM chat WHERE servertime >= ? AND servertime < ?)"
            " ORDER BY servertime, id LIMIT ?",
            bounds + bounds + (cap + 1,),
        ).fetchall()
        return [dict(r) for r in rows[:cap]]

    def bundle(self, start_ms: int, end_ms: int, source: str = "") -> dict[str, Any]:
        """The window as the bundle the player already reads, built by the same code as a CSV's."""
        import build_bundle

        cap = MAX_WINDOW_ROWS
        held = self.count_window(start_ms, end_ms)
        rows = self.window(start_ms, end_ms, limit=cap)
        # The query rounds the window outwards to the second, because `servertime` is compared as
        # text. The bundle builder filters again on the exact millisecond, and would cut back off
        # the report sitting on the boundary that the rounding was there to keep. One window, one
        # meaning: filter on the same bounds the rows were selected with.
        import math

        filter_start = math.floor(start_ms / 1000) * 1000
        filter_end = math.ceil(end_ms / 1000) * 1000
        out = build_bundle.bundle_from_rows(
            (
                {
                    "id": r["id"],
                    "uid": r["uid"],
                    "cot_type": r["cot_type"],
                    "how": r["how"],
                    "start": r["device_start"],
                    "time": r["device_time"],
                    "stale": r["stale"],
                    "servertime": r["servertime"],
                    "lat": r["lat"],
                    "lon": r["lon"],
                    "point_hae": r["hae"],
                    "point_ce": r["ce"],
                    "point_le": r["le"],
                    "detail": r["detail"],
                }
                for r in rows
            ),
            start_ms=filter_start,
            end_ms=filter_end,
            source=source or f"Pinecone's own archive ({self.path})",
        )
        # Declared as it was asked for, not as it was rounded.
        out["window"] = {"start": start_ms, "end": end_ms}
        if held > len(rows):
            # A window that was cut must say so. A silent cut is worse than a refusal: the counts
            # in this bundle otherwise certify a complete read of a window that was not complete.
            out["truncated"] = {"held": held, "returned": len(rows), "cap": cap}
            # Deliberately outside the four counts that reconcile against rows_read: those describe
            # what was read, and this describes what was never read at all.
            out["counts"]["rows_capped"] = held - len(rows)
        return out


def _text(value: Any) -> str | None:
    return None if value is None else str(value)


def _num(value: Any) -> float | None:
    try:
        return None if value is None or value == "" else float(value)
    except (TypeError, ValueError):
        return None


def _ms_to_text(ms: int, *, up: bool = False) -> str:
    """A window bound, to the second, rounded outwards.

    `servertime` is stored and compared as PostgreSQL's own text, and PostgreSQL emits a fraction
    only when there is one. A bound carrying ".000000" sorts after a report written at exactly that
    second with no fraction, and a bound truncated down sorts before every fractional report inside
    its final second. Both drop reports out of the window, so each bound is rounded away from the
    window: the start down, the end up. A window is therefore accurate to the second and never
    short, which is the right way round for a record whose argument is that it is complete. The
    cost is that two windows meeting at a boundary can overlap by up to a second and report the
    same reports twice. That is the right trade here, where a window is read to be watched; it is
    the wrong one if windows are ever stitched or exported, and that is when `servertime` should
    gain an integer epoch column beside the text rather than this being tuned further.
    """
    import math
    from datetime import datetime, timezone

    seconds = math.ceil(ms / 1000) if up else math.floor(ms / 1000)
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%d %H:%M:%S+00")
