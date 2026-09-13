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
import sqlite3
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

# How often the retention policy runs. The policy is in days, so a pass a day is enough and a
# pass every poll would be a DELETE over the whole archive every few seconds for nothing.
PRUNE_EVERY = 86_400.0


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


def retention_policy(env: Mapping[str, str]) -> dict[str, int]:
    """How long to keep each class of row, from the environment file, in days.

    `PINECONE_KEEP_DAYS` covers reports and messages; `PINECONE_KEEP_CONNECTION_DAYS` covers the
    connection log, which is kept for less because it is the more sensitive record. 0 means keep
    for ever, which is a choice an operator can make deliberately.

    Anything unreadable, or negative, falls back to the default rather than being treated as 0 or
    as a licence to delete. A typo in a configuration file must never be the reason an archive is
    emptied, and it must never silently turn retention off either.
    """
    import pinecone_archive

    out = dict(pinecone_archive.DEFAULT_RETENTION)
    for key, tables in (("PINECONE_KEEP_DAYS", ("report", "chat")), ("PINECONE_KEEP_CONNECTION_DAYS", ("connection",))):
        raw = env.get(key, "").strip()
        if not raw:
            continue
        try:
            days = int(raw)
        except ValueError:
            continue
        if days < 0:
            continue
        for table in tables:
            out[table] = days
    return out


def chosen_types(no_chat: bool, env: Mapping[str, str]) -> tuple[str, ...]:
    """Positions only when the flag or the environment file says so; otherwise everything the
    recorder takes. A value handed in, not a module global reassigned, so a test can hold the
    query it produces (the pre-UAT review of slice 5 found the global untestable)."""
    if no_chat or env.get("PINECONE_CHAT", "").strip().lower() in ("no", "false", "0", "off"):
        return ("a-",)
    return SOURCE_TYPES


# The group names a report was routed to, resolved on the server rather than kept as the bit
# vector it comes from. On a real TAK Server that vector is 32768 bits wide, so keeping it
# verbatim would add 32 KB to every row. `groups.bitpos` counts from
# the RIGHT-hand end of the vector, which is why the offset is `length(groups) - bitpos`; indexing
# from the left silently matches nothing and every row comes back with no groups at all.
#
# Null, not empty, when the server set no bits: "we do not know" and "no groups" are different
# answers and the picture has to be able to tell them apart.
GROUP_NAMES = (
    "(SELECT string_agg(g.name, ',' ORDER BY g.bitpos) FROM groups g"
    " WHERE {alias}.groups IS NOT NULL"
    " AND substring({alias}.groups::text FROM length({alias}.groups) - g.bitpos FOR 1) = '1')"
)

BASE_COLUMNS = (
    "id, uid, cot_type, how, start, time, stale, servertime,"
    " ST_Y(event_pt) AS lat, ST_X(event_pt) AS lon, point_hae, point_ce, point_le, detail"
)


def columns(with_groups: bool, alias: str = "t") -> str:
    """The report columns, with the group names only if this role may read the groups table.

    Found on a real box: the group-names subquery reads `groups`, and every box in the field runs
    a role with no grant on it until it updates. On such a box the whole report query failed with
    "permission denied for table groups", so reports and chat stopped recording. A missing grant
    must cost the group names and nothing else.

    Null is the honest value: the archive already defines a null `groups` as membership unknown,
    which is exactly what it is here.
    """
    if not with_groups:
        return BASE_COLUMNS + ", NULL AS groups"
    return BASE_COLUMNS + f", {GROUP_NAMES.format(alias=alias)} AS groups"


# The server's own answer to "which transactions might still be in flight".
#
# PostgreSQL takes an id from the sequence when a row is inserted and makes the row visible when
# its transaction commits, so a long transaction can hold a low id and commit after a shorter one
# that took a higher id has already been read. Reading strictly forward by id steps over it.
#
# pg_snapshot_xmin is the oldest transaction still in flight; everything below it has finished.
# Recording it each pass and re-reading anything whose xmin is at or above the PREVIOUS pass's
# horizon catches a late commit however far below the cursor its id fell, and stops re-reading it
# as soon as the horizon moves past.
#
# The modulo strips the xid8 epoch so the value is comparable with a row's 32-bit xmin. Residual,
# stated rather than hidden: across a transaction-id wraparound the comparison stops matching for
# one window and the read falls back to the id cursor alone, which is the behaviour before this
# change rather than something worse.
XID_HORIZON_SQL = "SELECT (pg_snapshot_xmin(pg_current_snapshot())::text::numeric % 4294967296)::bigint"


def late_commit_clause(since_xid: int) -> str:
    """The OR that widens an id read to catch a commit that landed behind the cursor.

    A widening, never a replacement. It cannot lose a row, and the writes are idempotent, so the
    worst a re-read costs is an insert that is ignored.
    """
    since = int(since_xid)
    return f" OR xmin::text::bigint >= {since}" if since > 0 else ""


Source = Callable[..., list[dict[str, Any]]]


def psql_source(table: str = "cot_router", types: tuple[str, ...] = SOURCE_TYPES) -> Source:
    """Reports after a cursor, straight out of the server's table, as CSV so a detail blob with
    pipes or newlines in it survives. The credential comes from the environment the unit provides.
    """

    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", table):
        raise ValueError(f"not a plain table name: {table!r}")

    # Whether this role may read the server's groups table. Learned from the first refusal rather
    # than probed, so a box that has the grant, which is every box after an update, pays nothing.
    groups_readable: list[bool] = []

    def read(after: int, limit: int, since_xid: int = 0) -> list[dict[str, Any]]:
        # The table name is checked above and every number is cast; nothing here comes from a
        # request or a file.
        where = (
            f"(id > {int(after)}{late_commit_clause(since_xid)})" f" AND {type_clause(types)} AND event_pt IS NOT NULL"
        )
        cols = columns(groups_readable[0] if groups_readable else True)
        sql = f"COPY (SELECT {cols} FROM {table} t WHERE {where} ORDER BY id LIMIT {int(limit)}) TO STDOUT WITH (FORMAT csv, HEADER)"  # noqa: S608
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
        if r.returncode != 0 and "permission denied for table groups" in r.stderr and not groups_readable:
            # A role created before the connection grants cannot read the groups table, and every
            # box in the field is one until it updates. Losing the group names is the right cost;
            # losing the reports is not. Learned here rather than probed, so the common case, a
            # role that has the grant, costs no extra round trip. Found on a real box.
            groups_readable.append(False)
            return read(after, limit, since_xid)
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


# Every name in this query is a literal in this file. Only the cursor and the limit vary, and both
# are cast to int at the call site.
CONNECTION_SQL = (
    "SELECT e.id, e.created_ts AS servertime, y.event_name AS event,"  # noqa: S608
    " c.callsign, c.uid, c.username, c.team, c.role, e.client_version,"
    f" {GROUP_NAMES.format(alias='e')} AS groups"
    " FROM client_endpoint_event e"
    " JOIN client_endpoint c ON c.id = e.client_endpoint_id"
    " JOIN connection_event_type y ON y.id = e.connection_event_type_id"
)


def psql_connection_source() -> Source:
    """Who was on the net, and when, out of the server's own connection log.

    This is the one part of the record that cannot be reconstructed later. The server prunes
    client_endpoint_event on its own schedule, so an event not taken today may simply not be there
    tomorrow; the reports, by contrast, are what Pinecone has been keeping since slice 1.

    Nothing here is derived: the event name is the server's, the timestamp is the server's, and
    the group names come from the server's own groups table.
    """

    def read(after: int, limit: int, since_xid: int = 0) -> list[dict[str, Any]]:
        # Every number is cast; every table and column name above is a literal in this file.
        where = f"(e.id > {int(after)}{late_commit_clause(since_xid).replace('xmin', 'e.xmin')})"
        sql = (
            f"COPY ({CONNECTION_SQL} WHERE {where} ORDER BY e.id LIMIT {int(limit)})"
            " TO STDOUT WITH (FORMAT csv, HEADER)"
        )
        env = dict(os.environ)
        env["PGTZ"] = "UTC"
        env["PGDATESTYLE"] = "ISO, YMD"
        try:
            r = subprocess.run(
                ["psql", "-At", "-c", sql], capture_output=True, text=True, timeout=120, env=env, check=False
            )
        except FileNotFoundError as exc:
            raise SourceError("psql is not on this box") from exc
        except subprocess.TimeoutExpired as exc:
            raise SourceError("the server's connection log did not answer within 120 seconds") from exc
        except OSError as exc:
            raise SourceError(f"psql could not be run: {exc.__class__.__name__}") from exc
        if r.returncode != 0:
            # A box whose role predates 0.8.0 has no SELECT on these four tables, and this is how
            # that shows: reported as a source failure on this table alone, so the reports and the
            # chat go on being recorded while the connection log says why it is not.
            raise SourceError(
                f"psql exited {r.returncode} reading the server's connection log: check the pinecone "
                "role is allowed to read the four connection tables, which install.sh grants "
                "from 0.8.0 on and a role created before it will not have"
            )
        try:
            return list(csv.DictReader(io.StringIO(r.stdout)))
        except csv.Error as exc:
            raise SourceError(f"the server's connection log could not be read: {exc}") from exc

    return read


def psql_xid_horizon() -> int:
    """The oldest transaction the server still has in flight, in xid space.

    Returns 0 when the server will not answer, which is the honest value: no horizon means no
    late-commit clause and the read falls back to the id cursor alone. An older PostgreSQL without
    pg_current_snapshot, or a role that cannot call it, must go on recording rather than stopping.
    """
    env = dict(os.environ)
    env["PGTZ"] = "UTC"
    try:
        r = subprocess.run(
            ["psql", "-At", "-c", XID_HORIZON_SQL], capture_output=True, text=True, timeout=30, env=env, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0
    if r.returncode != 0:
        return 0
    try:
        return int(r.stdout.strip())
    except ValueError:
        return 0


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


def all_sources(types: tuple[str, ...]) -> list[tuple[str, Source]]:
    """Everything a run reads: the CoT tables the type filter chooses, and the connection log.

    The connection log is not reached through `sources` because it is not CoT and the type filter
    has no opinion about it. --no-chat turns off a kind of CoT message; it does not turn off the
    record of who was on the net, and a run that recorded positions but silently stopped keeping
    connection events would lose the one thing the server deletes behind us.
    """
    return [*sources(types), ("connection", psql_connection_source())]


SOURCE_TABLE = {"report": "cot_router", "chat": "cot_router_chat", "connection": "client_endpoint_event"}


def head_for(table: str) -> Callable[[], int]:
    """Where the server's table has got to, for the table that feeds this one.

    The connection log's failure is given its own words because it is the one an operator will
    actually meet: a box whose role predates the connection grants fails here first, before the
    read, and "reading where the server's table has got to" does not tell them what to do.
    """
    source_table = SOURCE_TABLE.get(table, "cot_router")

    def head() -> int:
        try:
            return psql_head_id(source_table)
        except SourceError:
            if table != "connection":
                raise
            raise SourceError(
                "the connection log cannot be read: check the pinecone role is allowed to read the "
                "four connection tables, which install.sh grants from 0.8.0 on and a role created "
                "before it will not have. Positions and chat are unaffected."
            ) from None

    return head


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
    # A high-water mark means this table has recorded before, even if retention has since taken
    # every row it held. Without that check a pruned-empty archive looks like a fresh install and
    # the next pass reads the server's whole history again.
    if archive.get_meta(key(table, "cursor_floor")) or archive.count(table) or archive.high_water(table):
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


def _stored_horizon(archive: Any, table: str) -> int:
    """The horizon the last successful pass over this table saw, or 0 if there has not been one."""
    try:
        return int(archive.get_meta(archive.key(table, "xid_horizon")) or 0)
    except ValueError:
        return 0


def poll_once(
    source: Source,
    archive: Any,
    free_bytes: Callable[[], int] | None = None,
    floor_bytes: int = DEFAULT_FLOOR_BYTES,
    batch: int = DEFAULT_BATCH,
    head_id: Callable[[], int] | None = None,
    backfill: bool = True,
    table: str = "report",
    xid_horizon: Callable[[], int] | None = None,
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
        # The horizon is taken BEFORE the read, and the read asks from the PREVIOUS pass's horizon.
        # A transaction in flight during the last read is exactly the one that must be re-read, and
        # by now it may have finished; taking this pass's horizon to read with would skip it.
        now_xid = 0
        if xid_horizon is not None:
            try:
                now_xid = int(xid_horizon())
            except SourceError:
                now_xid = 0
        since_xid = _stored_horizon(archive, table)
        rows = (
            source(archive.read_from(CURSOR_LAG, table), batch, since_xid)
            if since_xid
            else source(archive.read_from(CURSOR_LAG, table), batch)
        )
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
    # Only now, after a read that succeeded. Advancing the horizon on a failed pass would step over
    # the very transactions this exists to catch.
    if now_xid:
        archive.set_meta(archive.key(table, "xid_horizon"), str(now_xid))
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


def _prune_now(archive: Any, policy: dict[str, int]) -> None:
    """Apply the retention policy, and never let it take the recorder down with it.

    A failed prune is a reason to say so and carry on recording, not to stop: losing the record
    because the deletion of old rows went wrong would be the wrong way round.
    """
    try:
        removed = archive.prune(policy)
    except sqlite3.Error as e:
        print(f"retention did not run: {e.__class__.__name__}", flush=True)
        return
    if any(removed.values()):
        gone = ", ".join(f"{n} {k}" for k, n in removed.items() if n)
        print(f"retention removed {gone}", flush=True)


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
    reads = all_sources(types)
    policy = retention_policy(os.environ)
    print(f"recording into {archive_path}, from id {archive.cursor()}: {' and '.join(n for n, _ in reads)}", flush=True)
    kept = ", ".join(f"{k} {v} days" if v else f"{k} for ever" for k, v in policy.items())
    print(f"keeping {kept}", flush=True)
    # Run once at startup so a box that is restarted more often than daily still prunes, and so an
    # operator who has just changed the policy sees it take effect rather than waiting a day.
    _prune_now(archive, policy)
    next_prune = time.monotonic() + PRUNE_EVERY
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
                xid_horizon=psql_xid_horizon,
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
                    xid_horizon=psql_xid_horizon,
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
            if time.monotonic() >= next_prune:
                _prune_now(archive, policy)
                next_prune = time.monotonic() + PRUNE_EVERY
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
        reads = all_sources(types)
        results = [
            poll_once(
                src,
                archive,
                floor_bytes=a.floor_mb * 1024**2,
                head_id=head_for(name),
                backfill=backfill,
                table=name,
                xid_horizon=psql_xid_horizon,
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
