#!/usr/bin/env python3
"""The recorder: Pinecone's own continuous record of what the TAK Server routed.

Runs as its own unit, from install, whenever the server is up (D2). It reads the server's table
forward from its own cursor and writes each report once into the archive (D5). It never interprets
a report and never writes back to the server.

The source sits behind a callable so the live subscription on port 8089 can replace the table read
later without the archive or the player noticing. That decision waits on the firehose test.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import io
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from typing import Any

# A CoT detail blob can be long, and the default field limit is 128 KB. build_bundle sets the same
# limit for the same reason; the recorder was left on the default, where an over-long field raised
# an error that was neither a source failure nor a programmer error, so the batch holding it was
# retried for ever while the page went on reporting health.
csv.field_size_limit(1 << 26)

DEFAULT_FLOOR_BYTES = 512 * 1024 * 1024  # keep this much free; the box runs TAK Server too
DEFAULT_BATCH = 2000
DEFAULT_INTERVAL = 10.0

# How far back below the high-water mark each pass re-reads.
#
# The source table's id comes from a sequence, and a sequence value is handed out at INSERT time,
# before the transaction commits. So a row with a lower id can become visible after one the
# recorder has already read, and a plain "id > cursor" would step over it and never come back. The
# writes are idempotent on that id, so re-reading a band below the cursor costs a few duplicate
# reads and closes the gap. This is the whole argument for the record being complete (D5).
CURSOR_LAG = 500

# The pause between catch-up batches. A backfill reads the server's own table, which is the
# database Pinecone is meant to sit lightly beside, so it is deliberately not as fast as the
# database will go: at 2,000 rows a batch this is roughly ten thousand reports a second, so the
# a box's 150,000 rows take under a minute and nothing else on the box notices.
CATCH_UP_PAUSE = 0.2


class SourceError(RuntimeError):
    """The server's table could not be read. Carries why, never the credential."""


# What the recorder asks the server for. Position reports, and since slice 5 GeoChat, which is the
# same stream with a different type: a message is worth exactly as much to a debrief as a position,
# and "I told you at half past" is settled by looking at it on the same timeline. Both are literal
# prefixes; nothing here comes from a request or a file.
SOURCE_TYPES: tuple[str, ...] = ("a-", "b-t-f")


def type_clause(types: tuple[str, ...] = SOURCE_TYPES) -> str:
    """The SQL for the types asked for, built only from the literals above."""
    parts = [f"cot_type LIKE '{t}%'" for t in types]
    return "(" + " OR ".join(parts) + ")"


def chosen_types(no_chat: bool, env: Mapping[str, str]) -> tuple[str, ...]:
    """Positions only when the flag or the environment file says so; otherwise everything the
    recorder takes. A value handed in, not a module global reassigned, so a test can hold the
    query it produces (the pre-UAT review of slice 5 found the global untestable)."""
    if no_chat or env.get("PINECONE_CHAT", "").strip().lower() in ("no", "false", "0", "off"):
        return ("a-",)
    return SOURCE_TYPES


COLUMNS = (
    "id, uid, cot_type, how, start, time, stale, servertime,"
    " ST_Y(event_pt) AS lat, ST_X(event_pt) AS lon, point_hae, point_ce, point_le, detail"
)

Source = Callable[[int, int], list[dict[str, Any]]]


def psql_source(table: str = "cot_router", types: tuple[str, ...] = SOURCE_TYPES) -> Source:
    """Reports after a cursor, straight out of the server's table, as CSV so a detail blob with
    pipes or newlines in it survives. The credential comes from the environment the unit provides.
    """

    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", table):
        raise ValueError(f"not a plain table name: {table!r}")

    def read(after: int, limit: int) -> list[dict[str, Any]]:
        # The table name is checked above and the two numbers are cast; nothing here comes from a
        # request or a file.
        where = f"id > {int(after)} AND {type_clause(types)} AND event_pt IS NOT NULL"
        sql = f"COPY (SELECT {COLUMNS} FROM {table} WHERE {where} ORDER BY id LIMIT {int(limit)}) TO STDOUT WITH (FORMAT csv, HEADER)"  # noqa: S608
        env = dict(os.environ)
        env["PGTZ"] = "UTC"
        # Windows are compared as text, so the server must emit ISO whatever its own DateStyle is.
        env["PGDATESTYLE"] = "ISO, YMD"
        try:
            r = subprocess.run(
                ["psql", "-At", "-c", sql], capture_output=True, text=True, timeout=120, env=env, check=False
            )
        except FileNotFoundError as exc:
            raise SourceError("psql is not on this box") from exc
        except subprocess.TimeoutExpired as exc:
            raise SourceError("the server's table did not answer within 120 seconds") from exc
        except OSError as exc:
            raise SourceError(f"psql could not be run: {exc.__class__.__name__}") from exc
        if r.returncode != 0:
            # psql's own stderr is not repeated: it can carry the connection string, and this line
            # reaches the journal and the page. The exit status says enough to act on.
            raise SourceError(
                f"psql exited {r.returncode} reading the server's table: check the database is up, "
                "and that the pinecone role and its password in /etc/pinecone/pinecone.env are still good"
            )
        try:
            return list(csv.DictReader(io.StringIO(r.stdout)))
        except csv.Error as exc:
            # Reported as a source failure, not left to the blanket handler: a row that cannot be
            # parsed sits inside the same window every pass, so an unreported failure here is a
            # recorder that never advances and never says why.
            raise SourceError(f"the server's rows could not be read: {exc}") from exc

    return read


def psql_head_id(table: str = "cot_router") -> int:
    """The highest id the server's table has reached. Read once, on a first run, so the record
    starts where it starts."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", table):
        raise ValueError(f"not a plain table name: {table!r}")
    env = dict(os.environ)
    env["PGTZ"] = "UTC"
    env["PGDATESTYLE"] = "ISO, YMD"
    try:
        r = subprocess.run(
            ["psql", "-At", "-c", f"SELECT coalesce(max(id), 0) FROM {table}"],  # noqa: S608 - checked above
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SourceError("could not read where the server's table has got to") from exc
    if r.returncode != 0:
        raise SourceError(f"psql exited {r.returncode} reading where the server's table has got to")
    return int((r.stdout.strip() or "0").splitlines()[0])


def sources(types: tuple[str, ...]) -> list[tuple[str, Source]]:
    """The tables a run reads, by the archive table each feeds. Positions come from cot_router.
    GeoChat comes from cot_router_chat: on TAK Server 5.8 it never lands in cot_router at all, and a
    recorder that asked cot_router for b-t-f recorded no chat on a real server while every test
    passed on synthetic rows (found in use, 5 September 2026)."""
    positions = tuple(t for t in types if t != "b-t-f") or ("a-",)
    out: list[tuple[str, Source]] = [("report", psql_source("cot_router", types=positions))]
    if "b-t-f" in types:
        out.append(("chat", psql_source("cot_router_chat", types=("b-t-f",))))
    return out


def head_for(table: str) -> Callable[[], int]:
    """Where the server's table has got to, for the table that feeds this one."""
    source_table = "cot_router_chat" if table == "chat" else "cot_router"
    return lambda: psql_head_id(source_table)


def seed_if_empty(archive: Any, head_id: Callable[[], int], backfill: bool = True, table: str = "report") -> int | None:
    """Decide where a record with nothing in it starts.

    By default it starts at the beginning of whatever the server still holds, so an estate that has
    been running for months keeps its exercises within a debrief's reach (decided 4 September
    2026). The floor is set to zero and the target is remembered, so the page can say how far
    the catch-up has to go.

    With `backfill` false it starts at the server's current position instead, which is the older
    behaviour and still the right one for an operator who does not want the history.

    Called on every pass, not once at startup: an archive can become empty again underneath a
    running recorder, and a floor only set at startup is silently lost exactly when it is doing its
    job. Raises SourceError if the server cannot be asked, which is the caller's to report rather
    than to die on. Returns the id it started from, or None if the record was already going.
    """
    key = archive.key
    if archive.get_meta(key(table, "cursor_floor")) or archive.count(table):
        return None
    where = head_id()
    if backfill:
        archive.set_floor(0, table)
        archive.set_meta(key(table, "backfill_target"), str(where))
        archive.set_meta(key(table, "backfill_done"), "no" if where else "yes")
        return 0
    archive.set_floor(where, table)
    archive.set_meta(key(table, "backfill_target"), "")
    archive.set_meta(key(table, "backfill_done"), "yes")
    return where


def free_space(path: str) -> int:
    """Free bytes, or -1 when it cannot be read. Not 0: a failed statvfs is not a full disk, and
    reporting it as one told the operator the box was full when it was not."""
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return -1


def _utcnow() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S+00")


def poll_once(
    source: Source,
    archive: Any,
    free_bytes: Callable[[], int] | None = None,
    floor_bytes: int = DEFAULT_FLOOR_BYTES,
    batch: int = DEFAULT_BATCH,
    head_id: Callable[[], int] | None = None,
    backfill: bool = True,
    table: str = "report",
) -> dict[str, Any]:
    """One pass over one table: read from just below the archive's own high-water mark, write what
    is new, and stamp a heartbeat whether or not anything came back. The report table's state keys
    are the ones the page has always read; the chat table's are prefixed.

    The heartbeat is the point of the stamp: without it a healthy recorder on a quiet net, a
    crashed one and a stopped one all look identical on the page.

    The disk is checked before every batch, not once at startup, because the box this runs on is
    also running TAK Server and filling its disk would take the server down with it.
    """
    if batch <= CURSOR_LAG:
        # Otherwise every pass refills itself with the band it just re-read and the cursor never
        # moves, while the page goes on saying it is recording.
        raise ValueError(f"batch of {batch} must be larger than the cursor lag of {CURSOR_LAG}")
    key = archive.key
    archive.reopen_if_gone()  # every pass, not only a pass that writes
    checked = _utcnow()
    archive.set_meta("last_checked", checked)  # a heartbeat, written every pass, empty batch or not
    free = (free_bytes or (lambda: free_space(os.path.dirname(archive.path) or ".")))()
    if 0 <= free < floor_bytes:
        reason = (
            f"not recording: only {free // 1024**2} MB of room left on this box, "
            f"below the floor of {floor_bytes // 1024**2} MB it keeps free for TAK Server"
        )
        archive.set_meta(key(table, "recording"), "no")
        archive.set_meta(key(table, "reason"), reason)
        return {
            "recorded": 0,
            "read": 0,
            "seeded": None,
            "recording": False,
            "reason": reason,
            "free_bytes": free,
            "cursor": archive.cursor(table),
            "last_checked": checked,
        }
    # Seeding and reading both talk to the server, so both fail the same way and are reported the
    # same way. Read from below the high-water mark, not from it. See CURSOR_LAG.
    #
    # `seeded` is set before the try, not inside it: a pass whose seed succeeds and whose read then
    # fails has set the floor, and reporting None would lose the one line that says where the
    # record started, on the only pass that could ever say it.
    seeded: int | None = None
    try:
        seeded = seed_if_empty(archive, head_id, backfill=backfill, table=table) if head_id is not None else None
        rows = source(archive.read_from(CURSOR_LAG, table), batch)
    except SourceError as e:
        reason = f"not recording: {e}"
        archive.set_meta(key(table, "recording"), "no")
        archive.set_meta(key(table, "reason"), reason)
        return {
            "recorded": 0,
            "read": 0,
            "seeded": seeded,
            "recording": False,
            "reason": reason,
            "free_bytes": free,
            "cursor": archive.cursor(table),
            "last_checked": checked,
        }
    written = archive.record(rows, table)
    # The catch-up is over when a pass comes back with less than it asked for: there is nothing
    # left behind the cursor, so the record has reached the present.
    if len(rows) < batch and archive.get_meta(key(table, "backfill_done")) == "no":
        archive.set_meta(key(table, "backfill_done"), "yes")
    note = "" if free >= 0 else "recording, but the free space on this box could not be read"
    archive.set_meta(key(table, "recording"), "yes")
    archive.set_meta(key(table, "reason"), note)
    return {
        "recorded": written,
        "seeded": seeded,
        "read": len(rows),
        "recording": True,
        "reason": note,
        "free_bytes": free,
        "cursor": archive.cursor(table),
        "last_checked": checked,
    }


def catch_up(
    source: Source,
    archive: Any,
    head_id: Callable[[], int] | None = None,
    pause: Callable[[float], None] = time.sleep,
    batch: int = DEFAULT_BATCH,
    every: float = CATCH_UP_PAUSE,
) -> int:
    """Read forward until the record has reached the present, pausing between batches.

    Separate from the poll loop because it has a different job: the loop waits for new reports,
    this one works through reports that are already there. The pause is what makes it safe to run
    against a server carrying an exercise. Returns how many were written.
    """
    written = 0
    while True:
        result = poll_once(source, archive, free_bytes=lambda: 1 << 60, batch=batch, head_id=head_id)
        written += result["recorded"]
        if not result["recording"]:
            return written
        if result["read"] < batch:
            return written
        pause(every)


def report_failure(archive: Any, exc: BaseException) -> str:
    """Put a failed pass on the page.

    The heartbeat says the unit is alive. This says it is alive and not working, which is a
    different and more useful thing to read at two in the morning. Only a source failure used to be
    reported, so anything else left the page saying "recording" with a heartbeat that kept moving.
    """
    reason = f"not recording: the last pass failed: {exc.__class__.__name__}: {exc}"
    with contextlib.suppress(Exception):  # the archive itself may be what failed
        archive.set_meta("recording", "no")
        archive.set_meta("reason", reason)
    return reason


def run(
    archive_path: str,
    interval: float = DEFAULT_INTERVAL,
    floor_bytes: int = DEFAULT_FLOOR_BYTES,
    batch: int = DEFAULT_BATCH,
    backfill: bool = True,
    catch_up_pause: float = CATCH_UP_PAUSE,
    types: tuple[str, ...] = SOURCE_TYPES,
) -> int:
    import pinecone_archive

    archive = pinecone_archive.Archive(archive_path)
    reads = sources(types)
    print(f"recording into {archive_path}, from id {archive.cursor()}: {' and '.join(n for n, _ in reads)}", flush=True)
    was_recording = True
    while True:
        try:
            result = poll_once(
                reads[0][1],
                archive,
                floor_bytes=floor_bytes,
                batch=batch,
                head_id=head_for("report"),
                backfill=backfill,
            )
            for name, src in reads[1:]:
                # Chat is a second table with its own cursor, polled after the positions. Its
                # failure is reported under its own keys and does not stop the positions.
                more = poll_once(
                    src,
                    archive,
                    floor_bytes=floor_bytes,
                    batch=batch,
                    head_id=head_for(name),
                    backfill=backfill,
                    table=name,
                )
                result["recorded"] += more["recorded"]
                result["read"] = max(result["read"], more["read"])
        except Exception as e:
            # The batch guard is a programmer error rather than a running condition, and retrying it
            # forever would turn a refusal to start into a unit that says "will try again" until
            # somebody reads the journal. Everything else is a running condition and is retried.
            if isinstance(e, ValueError) and "cursor lag" in str(e):
                raise
            report_failure(archive, e)
            print(f"poll failed, will try again: {e}", flush=True)
            was_recording = False
            time.sleep(interval)
            continue
        if result["seeded"] is not None:
            target = archive.get_meta("backfill_target")
            if backfill and target:
                print(
                    # `target` is the highest row id the server's table has reached, not a count of
                    # reports: most rows in it are not position reports at all. Say the id, because
                    # that is what is known, rather than dressing it up as a number of reports.
                    f"first run: taking the history the server still holds, oldest first, up to "
                    f"id {target}, {batch} at a time with {catch_up_pause}s between batches.",
                    flush=True,
                )
            else:
                print(
                    f"first run: starting the record at id {result['seeded']}. What the server "
                    "already holds from before now is not being taken.",
                    flush=True,
                )
        if result["recorded"]:
            print(f"recorded {result['recorded']}, cursor now {result['cursor']}", flush=True)
        if result["recording"] != was_recording:
            print(result["reason"] or "recording again", flush=True)
            was_recording = result["recording"]
        # A full batch means there is more waiting. Keyed on rows read, not rows written, because
        # catching up re-reads the lag band, most of which is already held. The catch-up pause is
        # short but not zero: this reads the server's own database, and going as fast as it will
        # answer is how a backfill becomes everybody else's problem.
        time.sleep(catch_up_pause if result["read"] >= batch else interval)


def main() -> int:
    ap = argparse.ArgumentParser(description="Record what the TAK Server routes into Pinecone's own archive.")
    ap.add_argument("--archive", default="/var/lib/pinecone/archive/pinecone.db")
    ap.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    ap.add_argument("--floor-mb", type=int, default=DEFAULT_FLOOR_BYTES // 1024**2)
    ap.add_argument("--once", action="store_true", help="one pass, then stop (for a check)")
    ap.add_argument(
        "--no-chat",
        action="store_true",
        help="record position reports only; PINECONE_CHAT=no in /etc/pinecone/pinecone.env does the same and survives an update",
    )
    ap.add_argument(
        "--no-backfill",
        action="store_true",
        help="start the record at the server's current position instead of taking the history it "
        "still holds; PINECONE_BACKFILL=no in /etc/pinecone/pinecone.env does the same and survives an update",
    )
    ap.add_argument(
        "--catch-up-pause",
        type=float,
        default=CATCH_UP_PAUSE,
        help="seconds between catch-up batches, so a backfill does not hold the database down",
    )
    a = ap.parse_args()
    # Whether to take the history is configuration, not a command-line habit, so it is read from
    # the environment file the unit already carries. That file survives an update; a flag edited
    # into the unit would not, which is the fault this repository has just spent an evening fixing.
    env_backfill = os.environ.get("PINECONE_BACKFILL", "").strip().lower()
    backfill = not a.no_backfill and env_backfill not in ("no", "false", "0", "off")
    types = chosen_types(a.no_chat, os.environ)
    if a.once:
        import pinecone_archive

        archive = pinecone_archive.Archive(a.archive)
        reads = sources(types)
        results = [
            poll_once(
                src, archive, floor_bytes=a.floor_mb * 1024**2, head_id=head_for(name), backfill=backfill, table=name
            )
            for name, src in reads
        ]
        for (name, _), result in zip(reads, results, strict=True):
            print(name, result)
        return 0 if results[0]["recording"] else 1
    return run(
        a.archive,
        a.interval,
        a.floor_mb * 1024**2,
        backfill=backfill,
        catch_up_pause=a.catch_up_pause,
        types=types,
    )


if __name__ == "__main__":
    sys.exit(main())
