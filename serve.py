#!/usr/bin/env python3
"""Local server for the Pinecone spike. Stdlib only. Loopback only.

  python3 serve.py [--port 8765] [--tiles /path/to/basemap.mbtiles] [--data data]

Serves the player, vendored Leaflet, bundles from data/*.json, and raster
tiles straight out of an mbtiles file so the demo needs no internet.
"""

import argparse
import contextlib
import glob
import html
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import pinecone_discover as discover

HERE = os.path.dirname(os.path.abspath(__file__))


def read_version():
    try:
        with open(os.path.join(HERE, "VERSION")) as f:
            return f.read().strip()
    except OSError:
        return "0"


def run_update(*args):
    """Run update.sh and return (rc, {key: value}, raw output)."""
    try:
        clean_env = {
            k: v for k, v in os.environ.items() if not k.startswith("PG")
        }  # never hand the updater a credential
        r = subprocess.run(
            ["bash", os.path.join(HERE, "update.sh"), *args], capture_output=True, text=True, timeout=180, env=clean_env
        )
        out = r.stdout + r.stderr
    except subprocess.TimeoutExpired:
        return 124, {"error": "update timed out"}, ""
    kv = dict(line.split("=", 1) for line in out.splitlines() if "=" in line and " " not in line.split("=", 1)[0])
    return r.returncode, kv, out


DISCOVERY_PATH = os.environ.get("PINECONE_DISCOVERY") or (
    "/etc/pinecone/discovery.json" if os.path.exists("/etc/pinecone/discovery.json") else ""
)
_live_cache: dict[str, Any] = {"at": 0.0, "value": None}


def read_discovery() -> dict[str, Any] | None:
    if not DISCOVERY_PATH:
        return None
    try:
        with open(DISCOVERY_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _psql(sql: str) -> str:
    """One query with Pinecone's own role, from the environment the unit provides. Never a credential in argv."""
    if not os.environ.get("PGUSER"):
        return ""
    try:
        r = subprocess.run(["psql", "-At", "-c", sql], capture_output=True, text=True, timeout=20, check=False)
        return r.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def live_facts() -> dict[str, Any]:
    """Row count, oldest and newest receipt time, and the display timezone, read live and cached for 30 s."""
    now = time.monotonic()
    if _live_cache["value"] is not None and now - _live_cache["at"] < 30:
        return _live_cache["value"]
    out: dict[str, Any] = {"rows": None, "timezone": None, "available": False}
    got = _psql(
        "SELECT count(*), count(DISTINCT uid), to_char(min(servertime) AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS') || 'Z', to_char(max(servertime) AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS') || 'Z' FROM cot_router;"
    )
    parts = got.split("|")
    if len(parts) == 4 and parts[0].isdigit():
        out["rows"] = {"count": int(parts[0]), "uids": int(parts[1]), "oldest": parts[2], "newest": parts[3]}
        out["available"] = True
    tz = _psql("SHOW timezone;")
    if tz:
        out["timezone"] = tz.splitlines()[0]
    _live_cache.update(at=now, value=out)
    return out


def discovery_with_live() -> dict[str, Any] | None:
    d = read_discovery()
    if d is None:
        return None
    live = live_facts()
    out: dict[str, Any] = json.loads(json.dumps(d))
    if live["rows"]:
        out["rows"] = live["rows"]
    if live["timezone"]:
        out.setdefault("database", {})["timezone"] = live["timezone"]
    out["live"] = live["available"]
    return out


def status_page(d: dict[str, Any] | None, bind: str, port: int) -> str:
    with open(os.path.join(HERE, "static", "status.html"), encoding="utf-8") as f:
        tpl = f.read()
    e = html.escape
    if d is None:
        body = (
            "<p class=warn>Pinecone is not installed on this box: there is no discovery record. "
            "Run <code>sudo ./install.sh</code> from an unpacked release beside your TAK Server, "
            "or open <a href='/replay'>the replay player</a> to look at a bundle.</p>"
            "<dl>" + map_section(active_source()) + archive_section() + "</dl>"
        )
        return tpl.replace("{{version}}", e(read_version())).replace("{{body}}", body)
    t, db, r, c = d["tak"], d.get("database", {}), d.get("retention", {}), d.get("credential", {})
    rows = d.get("rows")
    ports = ", ".join(f"{p} {'listening' if on else 'closed'}" for p, on in (t.get("ports") or {}).items())
    ttls = (
        ", ".join(f"{k}: {'null' if v is None else v}" for k, v in (r.get("ttls") or {}).items())
        or "no dataRetentionMap"
    )
    files = "".join(
        f"<li><code>{e(path)}</code> mode {e(str(f.get('mode')))} {e(str(f.get('owner')))}"
        + (f" <span class=warn>finding: {e(f['finding'])}</span>" if f.get("finding") else "")
        + "</li>"
        for path, f in (d.get("files") or {}).items()
    )
    if rows and d.get("live"):
        rows_html = (
            f"<b>{rows['count']:,}</b> position reports from <b>{rows['uids']:,}</b> nodes, oldest {e(str(rows['oldest']))}, "
            f"newest {e(str(rows['newest']))} (read live with role <code>{e(str(c.get('role') or 'pinecone'))}</code>)"
        )
    elif rows:
        rows_html = (
            f"<b>{rows['count']:,}</b> position reports from <b>{rows['uids']:,}</b> nodes, oldest {e(str(rows['oldest']))}, "
            f"newest {e(str(rows['newest']))} <span class=warn>(as recorded by the installer at {e(str(d.get('discovered_at')))}; "
            "the live read did not answer)</span>"
        )
    else:
        rows_html = (
            "<span class=warn>live facts unavailable: the database did not answer with Pinecone's role, "
            "and the installer recorded no count</span>"
        )
    running = read_version()
    recorded = str((d.get("pinecone") or {}).get("version") or "")
    record_version = f"Pinecone {recorded}" if recorded else "an unrecorded version"
    stale_note = (
        f", and this page is Pinecone {e(running)}: the record is older than the running version, so re-run the installer to refresh it"
        if recorded and recorded != running
        else ""
    )
    if bind in ("127.0.0.1", "localhost", "::1"):
        exposure = (
            f"Bound to loopback only (<code>{e(bind)}:{port}</code>) with <b>no authentication</b>. From another machine: "
            f"<code>ssh -L {port}:127.0.0.1:{port} user@this-box</code>. To expose it on the network, re-run the installer "
            "with <code>--bind &lt;address&gt;</code>, which is remembered across updates, and open the port yourself; nothing "
            "here touches the firewall. Editing the unit file by hand works until the next update rewrites it."
        )
    else:
        exposure = (
            f"Bound to <code>{e(bind)}:{port}</code>, <b>reachable from the network, with no authentication</b>: anyone who can "
            "reach the port sees everything. Nothing here touches the firewall."
        )
    body = f"""
<dl>
<dt>TAK Server</dt><dd><b>{e(str(t.get('version') or 'version unknown'))}</b> (from {e(str(t.get('version_source')))}); <code>{e(str(t.get('unit')))}</code> is {e(str(t.get('unit_state')))}</dd>
<dt>Listening</dt><dd>{e(ports)}</dd>
<dt>Database</dt><dd><code>{e(str(db.get('host')))}:{e(str(db.get('port')))}/{e(str(db.get('database')))}</code>, found in <code>{e(str(db.get('source')))}</code>; display timezone <b>{e(str(db.get('timezone') or 'unknown'))}</b> (Pinecone reads everything as UTC)</dd>
<dt>History</dt><dd>{rows_html}</dd>
<dt>Retention</dt><dd>{e(ttls)}; <b>{'something is purged' if r.get('purges') else 'nothing is purged'}</b> (from <code>{e(str(r.get('source')))}</code>)</dd>
<dt>Configuration files</dt><dd><ul>{files}</ul></dd>
<dt>Credential</dt><dd>{e(str(c.get('statement') or ''))}</dd>
{map_section(active_source())}
{archive_section()}
<dt>Exposure</dt><dd>{exposure}</dd>
<dt>Discovered</dt><dd>{e(str(d.get('discovered_at')))} by the installer, run as root, using {e(record_version)}{stale_note}; live facts read by role <code>{e(str(c.get('role') or 'pinecone'))}</code></dd>
</dl>
<p><a href="/replay">Open the replay player</a> · <a href="/api/discovery">This report as JSON</a></p>
"""
    return tpl.replace("{{version}}", e(read_version())).replace("{{body}}", body)


def default_tiles():
    """First .mbtiles under maps/ or data/ beside this file, else PINECONE_TILES, else none."""
    for d in ("maps", "data"):
        found = sorted(glob.glob(os.path.join(HERE, d, "*.mbtiles")))
        if found:
            return found[0]
    return os.environ.get("PINECONE_TILES")


DEFAULT_TILES = default_tiles()
MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript",
    ".css": "text/css",
    ".json": "application/json",
    ".png": "image/png",
    ".jpg": "image/jpeg",
}


class Tiles:
    def __init__(self, path):
        self.path = path
        self.db = None
        self.meta = {}
        self.lock = threading.Lock()
        if path and os.path.exists(path):
            self.db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
            self.meta = dict(self.db.execute("SELECT name, value FROM metadata"))

    def get(self, z, x, y):
        if not self.db:
            return None
        y_tms = (1 << z) - 1 - y
        with self.lock:  # one sqlite connection, many server threads
            row = self.db.execute(
                "SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?", (z, x, y_tms)
            ).fetchone()
        return row[0] if row else None


MAPS_DIR = ""
STATE_DIR = ""
ARCHIVE_PATH = ""
_map_cache: dict[str, Any] = {"at": 0.0, "value": None}
_map_lock = threading.Lock()
_choice_cache: dict[str, Any] = {"at": 0.0, "value": ""}
_active_cache: dict[str, Any] = {"id": None, "tiles": None}
_tiles_cache: dict[str, Tiles] = {}


ARCHIVE_WINDOWS = (("last-1h", 3600), ("last-6h", 6 * 3600), ("last-24h", 24 * 3600))

PACKS_DIR = ""
_packs_lock = threading.Lock()

# ---- named moments -----------------------------------------------------------------------------
#
# "This, not the map, is the product." A moment is a time and a name, kept on the box so it is
# there for whoever opens the page next. The cap on promoted moments is a constant, not a setting:
# the point of a budget is that it is not negotiable mid-meeting. Six is three sustains and three
# improves, the shape the doctrine says a debrief should leave with.
PROMOTED_CAP = 6
MOMENT_NAME_LIMIT = 200
MOMENTS_LIMIT = 500
# A moment's time is a millisecond timestamp somewhere between the start of 2000 and the end of
# 2100. Anything else is a hand-typed link or a script, and is refused rather than rendered as NaN.
MOMENT_AT_MIN = 946_684_800_000
MOMENT_AT_MAX = 4_133_980_800_000
_moments_lock = threading.Lock()
_dismiss_lock = threading.Lock()
_records_lock = threading.Lock()
_proposals_cache_lock = threading.Lock()
_proposals_cache: dict[str, tuple[float, bytes]] = {}
PROPOSALS_CACHE_SECONDS = 60
PROPOSALS_CACHE_ENTRIES = 8
RECORD_BODY_CAP = 65536  # one save of a record: a few fields, never a file
RECORD_SHAPE = "odcr"  # read once at start from the environment file; the two shapes are pinecone_record.SHAPES


def moments_path() -> str:
    return os.path.join(STATE_DIR or HERE, "moments.json")


def load_moments() -> list[dict[str, Any]]:
    """The store, degraded to what can be read. An entry a hand edit has damaged is dropped rather
    than allowed to fail every read of the list, which the threat note promised and the first
    version did not do: a string or a null in `at` raised in the sort and the page went blank."""
    try:
        with open(moments_path(), encoding="utf-8") as fh:
            got = json.load(fh)
    except (OSError, ValueError):
        return []
    if not isinstance(got, list):
        return []
    kept: list[dict[str, Any]] = []
    for m in got:
        if not isinstance(m, dict) or not isinstance(m.get("id"), str) or not m["id"]:
            continue
        at = m.get("at")
        if isinstance(at, bool) or not isinstance(at, int) or not (MOMENT_AT_MIN <= at <= MOMENT_AT_MAX):
            continue
        if not isinstance(m.get("name"), str) or not m["name"].strip():
            continue
        m["promoted"] = bool(m.get("promoted"))
        kept.append(m)
    return kept


def save_moments(moments: list[dict[str, Any]]) -> None:
    """Written whole to a temporary name and renamed into place, so a crash mid-write leaves the
    previous file rather than half of a new one."""
    path = moments_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(moments, fh, separators=(",", ":"))
    os.replace(tmp, path)


def records_path() -> str:
    return os.path.join(STATE_DIR or HERE, "records.json")


def dismissed_path() -> str:
    return os.path.join(STATE_DIR or HERE, "dismissed.json")


# A proposal's identity is the first twelve hex characters of a hash of its kind, time and callsigns.
# Nothing else is accepted as one, so nothing else is ever written to the dismissals file.
DISMISS_ID = re.compile(r"^[0-9a-f]{12}$")


def all_overlays() -> list[dict[str, Any]]:
    """Every overlay from every pack this box has imported, for the proposals to read."""
    out: list[dict[str, Any]] = []
    for pack in imported_packs():
        record = read_pack(pack["uid"])
        if record:
            out.extend(record.get("overlays") or [])
    return out


def moments_summary(moments: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(moments, key=lambda m: (int(m.get("at", 0)), str(m.get("created", ""))))
    return {
        "moments": ordered,
        "promoted": sum(1 for m in moments if m.get("promoted")),
        "promoted_cap": PROMOTED_CAP,
        "limit": MOMENTS_LIMIT,
    }


def clean_name(raw: str) -> str | None:
    """Text, whatever it contains; only its length is judged. The page renders it as text."""
    name = (raw or "").strip()
    if not name:
        return None
    if len(name) > MOMENT_NAME_LIMIT:
        raise ValueError(f"a name is at most {MOMENT_NAME_LIMIT} characters")
    return name


def pack_dir(uid: str) -> str:
    """Where one pack's unpacked overlays live. The uid comes from a file somebody else made, so it
    is reduced to something that cannot be a path before it is used as one."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", uid)[:64].strip("-.") or "package"
    if safe.startswith("_"):
        safe = "pack-" + safe  # nothing a pack names may take a directory this server keeps for itself
    return os.path.join(PACKS_DIR, safe)


def imported_packs() -> list[dict[str, Any]]:
    """Every pack this box has imported, read back from where they were written."""
    out: list[dict[str, Any]] = []
    if not PACKS_DIR or not os.path.isdir(PACKS_DIR):
        return out
    for name in sorted(os.listdir(PACKS_DIR)):
        record = os.path.join(PACKS_DIR, name, "pack.json")
        if not os.path.exists(record):
            continue
        with contextlib.suppress(OSError, ValueError):
            with open(record, encoding="utf-8") as fh:
                d = json.load(fh)
            out.append({"uid": name, "name": d.get("name") or name, "overlays": len(d.get("overlays") or [])})
    return out


def read_pack(uid: str) -> dict[str, Any] | None:
    record = os.path.join(pack_dir(uid), "pack.json")
    if not os.path.exists(record):
        return None
    with contextlib.suppress(OSError, ValueError), open(record, encoding="utf-8") as fh:
        return dict(json.load(fh))
    return None


def import_pack(path: str) -> dict[str, Any]:
    """Import a mission pack from a path on this box. Refusals carry the rule that stopped them."""
    import pinecone_packages

    with _packs_lock:
        # One read, not two: nothing is ever written to disk by the reader, so the earlier probe
        # pass only ever established the uid and cost a full second parse of every pack.
        pack = pinecone_packages.read_package(path, os.path.join(PACKS_DIR, "_reading"))
        target = pack_dir(pack.uid)
        replaced = None
        existing = read_pack(pack.uid)
        if existing is not None and existing.get("name") != pack.name:
            # The uid came from a file somebody else made. A second pack claiming the same one
            # would otherwise replace the first without a word.
            replaced = existing.get("name")
        os.makedirs(target, exist_ok=True)
        record = pack.as_dict()
        if replaced:
            record["replaced"] = replaced
        tmp = os.path.join(target, "pack.json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(record, fh, separators=(",", ":"))
        os.replace(tmp, os.path.join(target, "pack.json"))
        return record


def archive_stats() -> dict[str, Any]:
    """What Pinecone's own record holds, and whether it is still being written to.

    Read fresh every time: the recorder is a separate process, so a cached answer would be a page
    telling an operator that recording is fine some seconds after it stopped.
    """
    import shutil

    out: dict[str, Any] = {
        "path": ARCHIVE_PATH,
        "count": 0,
        "first": None,
        "last": None,
        "bytes": 0,
        "last_run": "",
        "last_checked": "",
        "recording": False,
        "reason": "",
        "free_bytes": 0,
        "catching_up": False,
        "backfill_target": 0,
    }
    out["free_bytes"] = -1  # -1 is "could not read", which the section renders as such; 0 is a full disk
    with contextlib.suppress(OSError):
        out["free_bytes"] = shutil.disk_usage(os.path.dirname(ARCHIVE_PATH) or ".").free
    if not ARCHIVE_PATH or not os.path.exists(ARCHIVE_PATH):
        out["reason"] = "no archive on this box yet: the recorder has not run, or Pinecone was not installed with one"
        return out
    try:
        import pinecone_archive

        a = pinecone_archive.Archive(ARCHIVE_PATH, read_only=True)
        try:
            out.update(a.stats())
            out["recording"] = a.get_meta("recording") == "yes"
            out["reason"] = a.get_meta("reason")
            target = a.get_meta("backfill_target")
            out["backfill_target"] = int(target) if target.isdigit() else 0
            out["catching_up"] = bool(out["backfill_target"]) and a.get_meta("backfill_done") != "yes"
        finally:
            a.close()
    except Exception as e:
        out["reason"] = f"the archive did not answer: {e}"
    return out


def archive_bundles() -> list[dict[str, Any]]:
    """The recent windows the player can ask the archive for, each with its own count.

    Its own count, not the size of the whole archive: showing one figure against all three told the
    operator nothing about which window had anything in it.
    """
    stats = archive_stats()
    if not stats["count"]:
        return []
    now = int(time.time() * 1000)
    out: list[dict[str, Any]] = []
    try:
        import pinecone_archive

        a = pinecone_archive.Archive(ARCHIVE_PATH, read_only=True)
        try:
            for label, seconds in ARCHIVE_WINDOWS:
                start = now - seconds * 1000
                held = a.count_window(start, now)
                entry: dict[str, Any] = {
                    "name": f"archive:{label}",
                    "window": {"start": start, "end": now},
                    "reports": held,
                }
                if held > pinecone_archive.MAX_WINDOW_ROWS:
                    entry["capped_at"] = pinecone_archive.MAX_WINDOW_ROWS
                out.append(entry)
        finally:
            a.close()
    except Exception:
        return []
    return out


def archive_window(start_ms: int, end_ms: int) -> dict[str, Any]:
    """A window the operator named, rather than one of the three the page offers.

    A debrief is run over a period somebody names, which is almost never the last hour, six hours
    or day (decided 4 September 2026).
    """
    if not ARCHIVE_PATH or not os.path.exists(ARCHIVE_PATH):
        return {"error": "there is no archive on this box yet"}
    import pinecone_archive

    try:
        a = pinecone_archive.Archive(ARCHIVE_PATH, read_only=True)
    except Exception as e:
        return {"error": f"the archive could not be opened: {e}"}
    try:
        out = a.bundle(start_ms, end_ms, source="Pinecone's own archive, the window you chose")
        if not out["counts"]["rows_kept"]:
            # Told, not left to guess: an empty map and a broken query look identical otherwise.
            out["empty"] = True
            held = a.stats()
            out["archive_holds"] = {"first": held["first"], "last": held["last"], "count": held["count"]}
        return out
    except Exception as e:
        return {"error": f"the archive could not be read: {e}"}
    finally:
        a.close()


def archive_bundle(name: str) -> dict[str, Any] | None:
    """One of those windows, built by the same code that builds a bundle from a CSV."""
    label = name.split(":", 1)[1] if ":" in name else ""
    seconds = dict(ARCHIVE_WINDOWS).get(label)
    if seconds is None or not ARCHIVE_PATH or not os.path.exists(ARCHIVE_PATH):
        return None
    import pinecone_archive

    try:
        a = pinecone_archive.Archive(ARCHIVE_PATH, read_only=True)
    except Exception as e:
        return {"error": f"the archive could not be opened: {e}"}
    try:
        now = int(time.time() * 1000)
        return a.bundle(now - seconds * 1000, now, source=f"Pinecone's own archive, {label.replace('-', ' ')}")
    except Exception as e:
        return {"error": f"the archive could not be read: {e}"}
    finally:
        a.close()


def bundle_from_query(q: dict[str, list[str]], data_dir: str) -> tuple[dict[str, Any] | None, int, str]:
    """The bundle a query names, the way /bundle.json names one: a file in data/, one of the archive's
    recent windows, or a window with a start and an end. The status and the sentence come back with
    it so any route can answer as /bundle.json would."""
    wanted = q.get("name", [""])[0]
    if wanted == "archive":
        raw_start, raw_end = q.get("start", [""])[0], q.get("end", [""])[0]
        try:
            start_ms, end_ms = int(raw_start), int(raw_end)
        except ValueError:
            return None, 400, "start and end must each be a time in milliseconds"
        if start_ms >= end_ms:
            return None, 400, "the start of a window must come before its end"
        built = archive_window(start_ms, end_ms)
        if "error" in built:
            return None, 503, str(built["error"])
        return built, 200, ""
    if wanted.startswith("archive:"):
        named = archive_bundle(wanted)
        if named is None:
            return None, 404, "no such window in the archive"
        if "error" in named:
            return None, 503, str(named["error"])
        return named, 200, ""
    name = os.path.basename(wanted)
    if not name:
        return None, 404, "no bundle named"
    path = os.path.join(data_dir, name + ".json")
    if not os.path.exists(path):
        return None, 404, "no bundle by that name"
    try:
        with open(path, encoding="utf-8") as fh:
            got = json.load(fh)
    except (OSError, ValueError) as e:
        return None, 500, f"that bundle could not be read: {e}"
    if not isinstance(got, dict):
        return None, 500, "that file is not a bundle"
    return got, 200, ""


# The one online map (Spec 011, decided 5 September 2026). A constant, not a
# discovery: the box never fetches a tile from it, the browser does, and the picker says so.
ONLINE_SOURCES: list[dict[str, Any]] = [
    {
        "id": "online:osm",
        "kind": "online",
        "name": "OpenStreetMap",
        "url_template": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        "attribution": "\u00a9 OpenStreetMap contributors",
        "origin": "the internet, fetched by your browser; needs a network",
        "needs_network": True,
        "format": "png",
        "minzoom": 0,
        "maxzoom": 19,
        "bounds": None,
    }
]


def all_sources() -> list[dict[str, Any]]:
    """The online map first, then what this box carries or serves."""
    return [dict(o) for o in ONLINE_SOURCES] + list(map_sources()["sources"])


def map_probes() -> list[tuple[str, int]]:
    """Which local ports to ask for a tile-service listing. PINECONE_TILE_PROBES overrides."""
    override = os.environ.get("PINECONE_TILE_PROBES")
    if override is not None:
        out = []
        for item in override.split(","):
            host, _, port = item.strip().rpartition(":")
            if host and port.isdigit():
                out.append((host, int(port)))
        return out
    try:
        ss = subprocess.run(["ss", "-ltn"], capture_output=True, text=True, timeout=5, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    return discover.listening_tile_probes(ss)


def map_dirs() -> list[str]:
    seen, out = set(), []
    for d in [MAPS_DIR, *discover.MAP_DIRS]:
        if d and d not in seen and os.path.isdir(d):
            seen.add(d)
            out.append(d)
    return out


def map_sources(refresh: bool = False) -> dict[str, Any]:
    """What this box carries or serves.

    Discovery globs directories, opens every mbtiles and asks a local port, so it is cached and
    guarded: without the lock every tile request in flight at the cache boundary would start its
    own discovery at once.
    """
    now = time.monotonic()
    if not refresh and _map_cache["value"] is not None and now - _map_cache["at"] < 60:
        return _map_cache["value"]
    with _map_lock:
        now = time.monotonic()
        if not refresh and _map_cache["value"] is not None and now - _map_cache["at"] < 60:
            return _map_cache["value"]
        found = discover.find_map_sources(map_dirs(), map_probes())
        _map_cache.update(at=now, value=found)
        return found


def choice_path() -> str:
    return os.path.join(STATE_DIR or HERE, "map-choice.json")


def chosen_id() -> str:
    """The chosen source's id, read at most once a second so a tile request is not a file read."""
    now = time.monotonic()
    if now - _choice_cache["at"] < 1.0:
        return str(_choice_cache["value"])
    try:
        with open(choice_path(), encoding="utf-8") as f:
            value = str(json.load(f).get("id") or "")
    except (OSError, ValueError):
        value = ""
    _choice_cache.update(at=now, value=value)
    return value


def set_choice(source_id: str) -> None:
    os.makedirs(os.path.dirname(choice_path()) or ".", exist_ok=True)
    with open(choice_path(), "w", encoding="utf-8") as f:
        json.dump({"id": source_id, "chosen_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, f)
    _choice_cache.update(at=0.0, value=source_id)
    _active_cache.update(id=None, tiles=None)


def active_source() -> dict[str, Any] | None:
    """The map actually being drawn: the chosen one, else a file named on the command line, else the
    first this box can draw with. It must agree with active_tiles(), or the page says something
    untrue about which map you are looking at.
    """
    sources = all_sources()
    want = chosen_id()
    for s in sources:
        if s["id"] == want:
            return s
    named = getattr(H.tiles, "path", None)
    if named and getattr(H.tiles, "db", None):
        for s in sources:
            if s.get("path") == named:
                return s
        return {
            "kind": "mbtiles",
            "name": H.tiles.meta.get("name") or os.path.basename(named),
            "format": H.tiles.meta.get("format") or "png",
            "minzoom": None,
            "maxzoom": None,
            "bounds": None,
            "attribution": H.tiles.meta.get("attribution") or "",
            "path": named,
            "origin": "named on the command line",
            "id": discover.source_id({"kind": "mbtiles", "path": named}),
        }
    # Nothing else: a wrong map is worse than no map (Spec 011). The page asks.
    return None


def active_tiles() -> Tiles:
    """A reader for the active source when it is a file on this box.

    A file named on the command line (--tiles) is what the operator asked for, so it wins when no
    choice has been made; otherwise the chosen source does.
    """
    want = chosen_id()
    s = active_source()
    path = str(s.get("path")) if s and s.get("kind") == "mbtiles" else ""
    # Keyed on what was resolved as well as on the choice: while nothing is chosen the key would
    # otherwise stay empty, and a later default would be described but not drawn.
    if _active_cache["id"] == (want, path) and _active_cache["tiles"] is not None:
        return _active_cache["tiles"]
    if not path:
        tiles = H.tiles
    else:
        if path not in _tiles_cache:
            _tiles_cache[path] = Tiles(path)
        tiles = _tiles_cache[path]
    _active_cache.update(id=(want, path), tiles=tiles)
    return tiles


# How long the page waits before it stops taking the recorder's word for it. The recorder polls
# every 10 seconds, so three minutes of silence is a stopped or wedged unit, not a quiet net.
HEARTBEAT_STALE_SECONDS = 180


def _age_seconds(stamp: str) -> float | None:
    """Seconds since a `YYYY-MM-DD HH:MM:SS+00` stamp, or None if it cannot be read."""
    from datetime import datetime, timezone

    text = (stamp or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M:%S.%f%z"):
        try:
            when = datetime.strptime(text.replace("+00", "+0000"), fmt)
        except ValueError:
            continue
        return (datetime.now(timezone.utc) - when).total_seconds()
    return None


def _ago(seconds: float) -> str:
    if seconds < 0:
        return "just now"  # the two clocks disagree; saying "-5 seconds ago" helps nobody
    if seconds < 90:
        return f"{int(seconds)} seconds ago"
    if seconds < 5400:
        return f"{int(seconds // 60)} minutes ago"
    if seconds < 172800:
        return f"{int(seconds // 3600)} hours ago"
    return f"{int(seconds // 86400)} days ago"


def archive_section() -> str:
    """What Pinecone's own record holds, in the operator's terms, and whether the recorder is alive.

    The record and the recorder are two facts, not one. A page that only says how many reports it
    holds cannot tell a healthy recorder on a quiet net from one that stopped hours ago, and that
    is exactly the failure an operator needs to see.
    """
    e = html.escape
    a = archive_stats()
    free = (
        "free space on this box could not be read"
        if a["free_bytes"] < 0
        else f"{a['free_bytes'] // 1024**3} GB free on this box"
    )
    age = _age_seconds(str(a.get("last_checked") or ""))
    if age is None:
        beat = "<span class=warn>the recorder has never checked in on this box</span>"
    elif age > HEARTBEAT_STALE_SECONDS:
        beat = f"<span class=warn>the recorder has not checked in since {e(str(a['last_checked']))}, {e(_ago(age))}: it is stopped or wedged</span>"
    else:
        beat = f"last checked {e(_ago(age))}"
    catching = ""
    if a.get("catching_up"):
        # Progress is measured in the server's own row ids, because that is what is known: the
        # target is the highest id its table had reached, and most rows in it are not position
        # reports. Counting them would be a second query on every page load to say the same thing.
        target = int(a.get("backfill_target") or 0)
        at = int(a.get("cursor") or 0)
        pct = f", about {100 * at // target}% through" if target else ""
        catching = (
            # Both numbers are row ids in the server's table, and most rows in it are not position
            # reports at all. Calling them reports made the page say that 19 in 20 had been lost
            # when nothing had: "at the server's report 40,000 of 150,000 ... 2,000 recorded so
            # far" reads as catastrophe. Say row, because that is what it is.
            f"<br><b>Catching up on the history the server still holds</b>: at row {at:,} of "
            f"{target:,} in the server's table{pct}, {int(a['count']):,} position reports recorded "
            "so far. Most rows in that table are not position reports, so the two numbers are not "
            "comparable. Reports from before Pinecone was installed are TAK's copy of history, "
            "taken once; everything after is Pinecone's own."
        )
    if not a["count"]:
        return (
            f"<dt>The record</dt><dd><span class=warn>{e(str(a['reason'] or 'nothing recorded yet'))}</span>; "
            f"{beat}. {e(free)}.{catching}</dd>"
        )
    held = f"<b>{a['count']:,}</b> reports recorded and <b>{int(a.get('messages') or 0):,}</b> messages, {e(str(a['first']))} to {e(str(a['last']))}"
    size = f"{a['bytes'] / 1024**2:.1f} MB"
    if a["recording"] and a["reason"]:
        state = f"<span class=warn>{e(str(a['reason']))}</span>"
    elif a["recording"]:
        wrote_age = _age_seconds(str(a.get("last_run") or ""))
        wrote = (
            "nothing yet"
            if not a.get("last_run")
            else (_ago(wrote_age) if wrote_age is not None else str(a["last_run"]))
        )
        state = f"recording, last wrote {e(wrote)}"
    else:
        state = f"<span class=warn>not recording: {e(str(a['reason'] or 'the recorder has not run yet on this box'))}</span>"
    return (
        f"<dt>The record</dt><dd>{held}, {size} in <code>{e(str(a['path']))}</code>; {state}; {beat}. {e(free)}."
        f"{catching}"
        "<br>This is Pinecone's own copy, and it stays whatever the TAK Server's retention later deletes."
        "<br><span class=dim>Position reports and GeoChat messages are kept; an <code>a-*</code> event with no fix is not a track point.</span></dd>"
    )


def map_section(chosen: dict[str, Any] | None) -> str:
    """The operator's list of maps: the map's own name, where it came from, then the path or URL."""
    e = html.escape
    found = map_sources()
    rows = []
    for s in found["sources"]:
        where = s.get("path") or s.get("url_template") or ""
        zooms = (
            f" zoom {s['minzoom']} to {s['maxzoom']}"
            if s.get("minzoom") is not None and s.get("maxzoom") is not None
            else ""
        )
        mark = " <b>(in use)</b>" if chosen and s["id"] == chosen["id"] else ""
        button = (
            ""
            if chosen and s["id"] == chosen["id"]
            else f'<button class=choose data-id="{e(s["id"])}">use this</button>'
        )
        rows.append(
            f"<li><b>{e(str(s['name']))}</b>{mark}{zooms}<br><span class=dim>{e(str(s['origin']))}</span>"
            f"<br><code>{e(str(where))}</code> {button}</li>"
        )
    if not rows:
        return (
            "<dt>Map</dt><dd><span class=warn>No basemap found on this box.</span> Put an <code>.mbtiles</code> in "
            f"<code>{e(MAPS_DIR or 'the maps directory')}</code>, or point Pinecone at the tile server this estate "
            "already runs. Nothing is fetched from the internet.</dd>"
        )
    off_box = ""
    if chosen and chosen.get("url_template"):
        host = urlparse(str(chosen["url_template"])).hostname or ""
        off_box = (
            f"<br><span class=warn>Tiles for this map are fetched by your browser from <code>{e(host)}</code>, "
            "not through Pinecone.</span>"
        )
    return f"<dt>Map</dt><dd><ul class=maps>{''.join(rows)}</ul>{off_box}</dd>"


class H(BaseHTTPRequestHandler):
    tiles: "Tiles" = Tiles(None)
    data_dir: str = os.path.join(HERE, "data")
    tiles_url: str | None = None
    attribution: str | None = None
    bind: str = "127.0.0.1"
    port: int = 8765

    def log_message(self, fmt, *args):
        if "/tiles/" not in getattr(self, "path", ""):
            # The query string is dropped: it carries what an operator typed (a callsign, a time)
            # and nothing the journal needs (review note N7). Computed outside the f-string: Python
            # 3.10 refuses a backslash inside the braces, and the box runs 3.10.
            line = re.sub(r"\?[^ ]*", "", fmt % args)
            sys.stderr.write(f"{self.address_string()} {line}\n")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def send(self, code, body=b"", ctype="text/plain", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):  # browser gave up on a tile mid-pan
            self.wfile.write(body)

    def records_get(self, p: str) -> None:
        import pinecone_record

        with _records_lock:
            records = pinecone_record.load_records(records_path())
        if p == "/api/records":
            out = {
                "records": [pinecone_record.summary(r) for r in records],
                "cap": pinecone_record.RECORDS_CAP,
                "items_cap": pinecone_record.ITEMS_CAP,
                "doctrine": pinecone_record.DOCTRINE,
                "shape": RECORD_SHAPE,
            }
            return self.send(200, json.dumps(out).encode(), MIME[".json"])
        rid = p[len("/api/records/") :]
        found = next((r for r in records if r["id"] == rid), None)
        if found is None:
            return self.send(404, b"no record by that id")
        out = {
            **found,
            "items_cap": pinecone_record.ITEMS_CAP,
            "doctrine": pinecone_record.DOCTRINE,
            "shape": RECORD_SHAPE,
        }
        return self.send(200, json.dumps(out).encode(), MIME[".json"])

    def record_export(self, rid: str) -> None:
        import socket

        import pinecone_record

        with _records_lock:
            records = pinecone_record.load_records(records_path())
        found = next((r for r in records if r["id"] == rid), None)
        if found is None:
            return self.send(404, b"no record by that id")
        with _moments_lock:
            moments = load_moments()
        w = found["window"]
        inside = [m for m in moments if w["start"] <= int(m["at"]) < w["end"]]
        text = pinecone_record.render_markdown(
            found, inside, RECORD_SHAPE, socket.gethostname(), read_version(), int(time.time() * 1000)
        )
        return self.send(
            200,
            text.encode("utf-8"),
            "text/markdown; charset=utf-8",
            {
                "Content-Disposition": f'attachment; filename="pinecone-record-{rid}.md"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    def records_post(self, p: str) -> None:
        import pinecone_record

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self.send(400, b"the request's length is not a number")
        if length > RECORD_BODY_CAP:
            # Refused whole, never cut: four fields of 4,000 accented characters exceed the cap on
            # the wire, and the first version kept what fitted and answered 200 (review of slice 7).
            return self.send(
                413,
                f"That is more text than one save can carry. Each field holds about {pinecone_record.FIELD_LIMIT:,} "
                "characters; shorten the longest one and save again. Nothing you typed has been lost.".encode(),
            )
        body = self.rfile.read(length).decode("utf-8", "replace") if length > 0 else ""
        form = {k: v[0] for k, v in parse_qs(body, keep_blank_values=True).items()}
        parts = p.split("/")  # ["", "api", "records", rid?, "items" | "delete"?, iid?, "delete"?]
        with _records_lock:
            records = pinecone_record.load_records(records_path())
            try:
                if len(parts) == 3:
                    if len(records) >= pinecone_record.RECORDS_CAP:
                        return self.send(
                            409,
                            f"This box holds at most {pinecone_record.RECORDS_CAP} records. Delete some you no longer need.".encode(),
                        )
                    made = pinecone_record.new_record(
                        form.get("title", ""), form.get("start", ""), form.get("end", ""), form.get("objectives", "")
                    )
                    records.append(made)
                    self._save_records(records)
                    return self.send(200, json.dumps(pinecone_record.summary(made)).encode(), MIME[".json"])
                rid = parts[3]
                found = next((r for r in records if r["id"] == rid), None)
                if found is None:
                    return self.send(404, b"no record by that id")
                if len(parts) == 4:
                    if "title" in form:
                        found["title"] = pinecone_record.clean_title(form["title"])
                    if "objectives" in form:
                        found["objectives"] = pinecone_record.clean_text(
                            form["objectives"], "What was supposed to happen", pinecone_record.OBJECTIVES_LIMIT
                        )
                    self._save_records(records)
                    return self.send(200, json.dumps(pinecone_record.summary(found)).encode(), MIME[".json"])
                if parts[4] == "delete" and len(parts) == 5:
                    records = [r for r in records if r["id"] != rid]
                    self._save_records(records)
                    return self.send(200, json.dumps({"deleted": rid}).encode(), MIME[".json"])
                if parts[4] != "items":
                    return self.send(404, b"not found")
                if len(parts) == 5:
                    if len(found["items"]) >= pinecone_record.ITEMS_CAP:
                        return self.send(
                            409,
                            f"This record holds {pinecone_record.ITEMS_CAP} items at most. Delete one you no longer need, or "
                            "start another record. Doctrine's number is three sustains and three improves.".encode(),
                        )
                    if "moment" in form:
                        with _moments_lock:
                            matched = [m for m in load_moments() if m.get("id") == form["moment"]]
                        moment = matched[0] if matched else None
                        if moment is None:
                            return self.send(404, b"no moment by that id")
                        item = pinecone_record.item_from_moment(moment)
                    else:
                        item = pinecone_record.clean_item(form)
                    found["items"].append(item)
                    self._save_records(records)
                    return self.send(200, json.dumps(item).encode(), MIME[".json"])
                iid = parts[5]
                current: dict[str, Any] | None = next((i for i in found["items"] if i["id"] == iid), None)
                if current is None:
                    return self.send(404, b"no item by that id")
                if len(parts) == 7 and parts[6] == "delete":
                    found["items"] = [i for i in found["items"] if i["id"] != iid]
                    self._save_records(records)
                    return self.send(200, json.dumps({"deleted": iid}).encode(), MIME[".json"])
                if len(parts) != 6:
                    return self.send(404, b"not found")
                changed = pinecone_record.clean_item(form, existing=current)
                found["items"] = [changed if i["id"] == iid else i for i in found["items"]]
                self._save_records(records)
                return self.send(200, json.dumps(changed).encode(), MIME[".json"])
            except ValueError as e:
                return self.send(400, str(e).encode())
            except OSError:
                return self.send(500, b"the state directory cannot be written, so the record was not kept")

    def _save_records(self, records) -> None:
        import pinecone_record

        pinecone_record.save_records(records_path(), records)

    def moments_post(self, p: str) -> None:
        import uuid

        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(min(length, 8192)).decode("utf-8", "replace") if length else ""
        form = {k: v[0] for k, v in parse_qs(body).items()}
        with _moments_lock:
            moments = load_moments()
            if p == "/api/moments":
                try:
                    at = int(form.get("at", "").replace("_", "x"))  # int() would otherwise accept 1_000
                except ValueError:
                    return self.send(400, b"a moment needs a time, in milliseconds")
                if not (MOMENT_AT_MIN <= at <= MOMENT_AT_MAX):
                    return self.send(400, b"a moment's time is a millisecond timestamp between 2000 and 2100")
                try:
                    name = clean_name(form.get("name", ""))
                except ValueError as e:
                    return self.send(400, str(e).encode())
                if name is None:
                    return self.send(400, b"a moment needs a name")
                if len(moments) >= MOMENTS_LIMIT:
                    return self.send(409, f"this box holds at most {MOMENTS_LIMIT} moments".encode())
                made = {
                    "id": uuid.uuid4().hex[:12],
                    "at": at,
                    "name": name,
                    "promoted": False,
                    "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
                moments.append(made)
                try:
                    save_moments(moments)
                except OSError:
                    return self.send(500, b"the state directory cannot be written, so the moment was not kept")
                return self.send(200, json.dumps(made).encode(), MIME[".json"])
            parts = p.split("/")
            mid, action = parts[3], (parts[4] if len(parts) > 4 else "")
            found = next((m for m in moments if m.get("id") == mid), None)
            if found is None:
                return self.send(404, b"no moment by that id")
            if action == "delete":
                moments = [m for m in moments if m.get("id") != mid]
                try:
                    save_moments(moments)
                except OSError:
                    return self.send(500, b"the state directory cannot be written, so nothing was deleted")
                return self.send(200, json.dumps({"deleted": mid}).encode(), MIME[".json"])
            if action:
                return self.send(404, b"not found")
            if "name" in form:
                try:
                    name = clean_name(form["name"])
                except ValueError as e:
                    return self.send(400, str(e).encode())
                if name is None:
                    return self.send(400, b"a moment needs a name")
                found["name"] = name
            if "promoted" in form:
                want = form["promoted"].strip().lower() in ("yes", "true", "1", "on")
                promoted_now = sum(1 for m in moments if m.get("promoted") and m.get("id") != mid)
                if want and promoted_now >= PROMOTED_CAP:
                    return self.send(
                        409,
                        f"the budget is {PROMOTED_CAP} promoted moments and all {PROMOTED_CAP} are taken; "
                        "demote one to promote this".encode(),
                    )
                found["promoted"] = want
            try:
                save_moments(moments)
            except OSError:
                return self.send(500, b"the state directory cannot be written, so the change was not kept")
            return self.send(200, json.dumps(found).encode(), MIME[".json"])

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        p = u.path
        if p in ("/", "/replay"):
            return self.file(os.path.join(HERE, "static", "index.html"))
        if p == "/status":
            return self.send(200, status_page(discovery_with_live(), self.bind, self.port).encode(), MIME[".html"])
        if p == "/api/discovery":
            d = discovery_with_live()
            return self.send(
                404 if d is None else 200, json.dumps(d or {"error": "not installed"}).encode(), MIME[".json"]
            )
        if p == "/favicon.ico":
            return self.send(204)
        if p == "/version":
            return self.send(
                200,
                json.dumps(
                    {
                        "version": read_version(),
                        "repo": os.environ.get("PINECONE_REPO", "MilUX-Ltd/pinecone"),
                        "local": self.client_address[0] in ("127.0.0.1", "::1"),
                        "can_update": os.access(HERE, os.W_OK),
                        "git_checkout": os.path.isdir(os.path.join(HERE, ".git")),
                    }
                ).encode(),
                MIME[".json"],
            )
        if p == "/update/check":
            rc, kv, out = run_update("--check")
            return self.send(200, json.dumps({"rc": rc, **kv}).encode(), MIME[".json"])
        if p.startswith("/static/") or p.startswith("/vendor/"):
            name = os.path.basename(p)
            return self.file(os.path.join(HERE, p.split("/")[1], name))
        if p == "/bundles":
            out = []
            for f in sorted(glob.glob(os.path.join(self.data_dir, "*.json"))):
                try:
                    with open(f) as fh:
                        head = fh.read(4000)
                    w = json.loads(head[head.index('"window":') + 9 : head.index("}", head.index('"window":')) + 1])
                except Exception:
                    w = None
                out.append({"name": os.path.basename(f)[:-5], "window": w, "bytes": os.path.getsize(f)})
            # The archive's recent windows come first: they are what a debrief reaches for.
            return self.send(200, json.dumps(archive_bundles() + out).encode(), MIME[".json"])
        if p == "/bundle.json":
            wanted = q.get("name", [""])[0]
            if wanted == "archive" or wanted.startswith("archive:"):
                built, code, why = bundle_from_query(q, self.data_dir)
                if built is None:
                    return self.send(code, why.encode())
                return self.send(200, json.dumps(built, separators=(",", ":")).encode(), MIME[".json"])
            name = os.path.basename(wanted)
            if not name:
                return self.send(404, b"no bundle named")
            return self.file(os.path.join(self.data_dir, name + ".json"))
        if p == "/api/records" or (p.startswith("/api/records/") and p.count("/") == 3):
            return self.records_get(p)
        if p.startswith("/record/") and p.endswith(".md"):
            rid = p[len("/record/") : -len(".md")]
            if not DISMISS_ID.match(rid):
                return self.send(404, b"no record by that id")
            return self.record_export(rid)
        if p == "/api/proposals":
            import pinecone_proposals

            # A computed list is kept for a minute per query, keyed on the dismissals file's
            # modification time so a dismissal is never served back: at the window cap with a
            # hundred callsigns one request is ten seconds of CPU, unauthenticated (review note N2).
            try:
                stamp = os.path.getmtime(dismissed_path())
            except OSError:
                stamp = 0.0
            key = f"{u.query}|{stamp}"
            with _proposals_cache_lock:
                hit = _proposals_cache.get(key)
                if hit and time.monotonic() - hit[0] < PROPOSALS_CACHE_SECONDS:
                    return self.send(200, hit[1], MIME[".json"])
            bundle, code, why = bundle_from_query(q, self.data_dir)
            if bundle is None:
                return self.send(code, why.encode())
            overlays = all_overlays()
            with _dismiss_lock:
                gone = pinecone_proposals.load_dismissed(dismissed_path())
            proposals = pinecone_proposals.propose(bundle, overlays, gone)
            out = {
                "proposals": proposals,
                "count": len(proposals),
                "cap": pinecone_proposals.PROPOSALS_CAP,
                "dismissed": len(gone),  # on this box, every window
                "overlays": len(overlays),
                "rules": {
                    "co_location_m": pinecone_proposals.CO_LOCATION_M,
                    "co_location_ms": pinecone_proposals.CO_LOCATION_MS,
                    "silence_ms": pinecone_proposals.SILENCE_MS,
                    "watch_words": list(pinecone_proposals.WATCH_WORDS),
                },
            }
            body = json.dumps(out).encode()
            with _proposals_cache_lock:
                if len(_proposals_cache) >= PROPOSALS_CACHE_ENTRIES:
                    _proposals_cache.pop(next(iter(_proposals_cache)))
                _proposals_cache[key] = (time.monotonic(), body)
            return self.send(200, body, MIME[".json"])
        if p == "/api/where":
            import pinecone_proposals

            callsign = q.get("callsign", [""])[0].strip()
            if not callsign:
                return self.send(400, b"where-was needs a callsign")
            try:
                at = int(q.get("at", [""])[0].replace("_", "x"))
            except ValueError:
                return self.send(400, b"where-was needs a time, in milliseconds")
            if not (MOMENT_AT_MIN <= at <= MOMENT_AT_MAX):
                return self.send(400, b"where-was needs a millisecond timestamp between 2000 and 2100")
            bundle, code, why = bundle_from_query(q, self.data_dir)
            if bundle is None:
                return self.send(code, why.encode())
            answer = pinecone_proposals.where_was(bundle, callsign, at)
            return self.send(200, json.dumps(answer).encode(), MIME[".json"])
        if p == "/api/moments":
            with _moments_lock:
                return self.send(200, json.dumps(moments_summary(load_moments())).encode(), MIME[".json"])
        if p == "/api/packs":
            return self.send(200, json.dumps(imported_packs()).encode(), MIME[".json"])
        if p.startswith("/api/packs/") and p.endswith("/overlays"):
            uid = p[len("/api/packs/") : -len("/overlays")]
            pack = read_pack(uid)
            if pack is None:
                return self.send(404, b"no pack imported under that name")
            return self.send(200, json.dumps(pack["overlays"]).encode(), MIME[".json"])
        if p == "/api/archive":
            return self.send(200, json.dumps(archive_stats()).encode(), MIME[".json"])
        if p == "/api/maps":
            found = map_sources(refresh="refresh" in q)
            chosen = active_source()
            listed = {**found, "sources": [dict(o) for o in ONLINE_SOURCES] + list(found["sources"])}
            return self.send(
                200,
                json.dumps({**listed, "chosen": chosen["id"] if chosen else None}).encode(),
                MIME[".json"],
            )
        if p == "/tiles/meta":
            chosen = active_source()
            tiles = active_tiles()
            url = self.tiles_url or (chosen.get("url_template") if chosen else None)
            meta = dict(tiles.meta)
            if chosen and not meta:
                meta = {
                    "name": chosen.get("name"),
                    "format": chosen.get("format"),
                    "minzoom": chosen.get("minzoom"),
                    "maxzoom": chosen.get("maxzoom"),
                    "bounds": chosen.get("bounds"),
                    "attribution": chosen.get("attribution"),
                }
            return self.send(
                200,
                json.dumps(
                    {
                        "path": tiles.path,
                        "meta": meta,
                        "available": bool(tiles.db) or bool(url),
                        "url": url,
                        "attribution": self.attribution or (chosen or {}).get("attribution"),
                        "chosen": chosen["id"] if chosen else None,
                    }
                ).encode(),
                MIME[".json"],
            )
        if p.startswith("/tiles/"):
            try:
                z, x, y = (int(v) for v in p[7:].split(".")[0].split("/"))
            except ValueError:
                return self.send(400, b"bad tile")
            blob = active_tiles().get(z, x, y)
            if blob is None:
                return self.send(404, b"")
            fmt = active_tiles().meta.get("format", "png")
            return self.send(200, blob, "image/jpeg" if fmt in ("jpg", "jpeg") else "image/png")
        return self.send(404, b"not found")

    def do_POST(self):
        p = urlparse(self.path).path
        if p == "/api/moments" or (p.startswith("/api/moments/") and p.count("/") in (3, 4)):
            return self.moments_post(p)
        if p == "/api/records" or (p.startswith("/api/records/") and 3 <= p.count("/") <= 6):
            return self.records_post(p)
        if p.startswith("/api/proposals/") and p.endswith("/dismiss"):
            import pinecone_proposals

            pid = p[len("/api/proposals/") : -len("/dismiss")]
            if not DISMISS_ID.match(pid):
                return self.send(400, b"a proposal's identity is twelve hex characters")
            with _dismiss_lock:
                try:
                    pinecone_proposals.dismiss(dismissed_path(), pid)
                except OSError:
                    return self.send(500, b"the state directory cannot be written, so the dismissal was not kept")
            return self.send(200, json.dumps({"dismissed": pid}).encode(), MIME[".json"])
        if p == "/api/packs/import":
            import pinecone_packages

            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(min(length, 8192)).decode("utf-8", "replace") if length else ""
            wanted = parse_qs(body).get("path", [""])[0]
            if not wanted:
                return self.send(400, b"give the path of a data package on this box")
            try:
                record = import_pack(wanted)
            except pinecone_packages.NotAPackage as e:
                return self.send(400, str(e).encode())
            except pinecone_packages.BadPackage as e:
                return self.send(400, str(e).encode())
            except OSError as e:
                return self.send(400, f"that package could not be read: {e}".encode())
            return self.send(200, json.dumps(record).encode(), MIME[".json"])
        if p == "/api/maps/choose":
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(min(length, 4096)).decode("utf-8", "replace") if length else ""
            wanted = parse_qs(body).get("id", [""])[0]
            ids = {s["id"] for s in all_sources()}
            if not wanted or wanted not in ids:
                return self.send(
                    400,
                    json.dumps({"error": f"{wanted or 'that'} is not one of the sources this box found"}).encode(),
                    MIME[".json"],
                )
            set_choice(wanted)
            return self.send(200, json.dumps({"chosen": wanted}).encode(), MIME[".json"])
        if p != "/update/apply":
            return self.send(404, b"not found")
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            return self.send(
                403,
                json.dumps({"error": "updates can only be applied from the box itself (loopback)"}).encode(),
                MIME[".json"],
            )
        rc, kv, out = run_update("apply", *(["--force"] if os.environ.get("PINECONE_UPDATE_FORCE") else []))
        if rc == 0 and kv.get("updated"):

            def restart():
                os.chdir(HERE)
                # Restart in place after a verified update. argv is this process's own command line, not
                # request input; the request only reaches here from loopback and after the sha256 check.
                # nosemgrep: python.lang.security.audit.dangerous-os-exec-tainted-env-args.dangerous-os-exec-tainted-env-args
                os.execv(sys.executable, [sys.executable, *sys.argv])  # noqa: S606

            threading.Timer(1.0, restart).start()
        return self.send(
            200,
            json.dumps(
                {"rc": rc, "restarting": rc == 0 and bool(kv.get("updated")), **kv, "output": out[-2000:]}
            ).encode(),
            MIME[".json"],
        )

    def file(self, path):
        if not os.path.isfile(path):
            return self.send(404, b"not found")
        with open(path, "rb") as f:
            body = f.read()
        return self.send(200, body, MIME.get(os.path.splitext(path)[1], "application/octet-stream"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--bind", default="127.0.0.1", help="address to listen on; loopback unless you decide otherwise")
    ap.add_argument("--tiles", default=None, help="raster mbtiles file to draw, named by you; otherwise the page asks")
    ap.add_argument(
        "--tiles-url",
        default=None,
        help="XYZ tile URL template instead of an mbtiles, e.g. https://tile.openstreetmap.org/{z}/{x}/{y}.png (needs internet)",
    )
    ap.add_argument("--attribution", default=None, help="attribution text for --tiles-url")
    ap.add_argument("--data", default=os.path.join(HERE, "data"))
    ap.add_argument("--maps", default=None, help="directory holding .mbtiles basemaps and map-source definitions")
    ap.add_argument(
        "--state", default=None, help="where the map choice is remembered (default: beside the data directory)"
    )
    ap.add_argument("--archive", default=None, help="Pinecone's own record, written by the recorder")
    ap.add_argument("--packs", default=None, help="where imported mission packs are kept")
    a = ap.parse_args()
    # A file named on the command line is the operator's word and is drawn. Nothing else is
    # chosen for them: not the first file in the maps directory, not the first beside this
    # script. The page asks (Spec 011).
    tiles_path = a.tiles
    H.bind, H.port = a.bind, a.port
    global MAPS_DIR, STATE_DIR, ARCHIVE_PATH, PACKS_DIR, RECORD_SHAPE
    import pinecone_record

    RECORD_SHAPE = pinecone_record.chosen_shape(os.environ)
    MAPS_DIR = a.maps or ""
    STATE_DIR = a.state or os.path.dirname(os.path.abspath(a.data))
    PACKS_DIR = a.packs or os.path.join(STATE_DIR, "packs")
    default_archive = os.path.join(STATE_DIR, "archive", "pinecone.db")
    ARCHIVE_PATH = a.archive or (default_archive if os.path.exists(default_archive) else "")
    H.tiles = Tiles(None if a.tiles_url else tiles_path)
    H.data_dir = a.data
    H.tiles_url = a.tiles_url
    H.attribution = a.attribution
    if a.tiles_url:
        print(f"tiles: {a.tiles_url} (fetched by the browser; needs a network)")
    elif not tiles_path:
        print("tiles: none named; the page draws OpenStreetMap when the browser has a network, else asks for a pack")
    else:
        print(
            f"tiles: {a.tiles} {'(open, ' + H.tiles.meta.get('name', '?') + ')' if H.tiles.db else '(NOT FOUND, map will be blank; pass --tiles or --tiles-url)'}"
        )
    print(f"bundles in {a.data}: {[os.path.basename(f) for f in glob.glob(os.path.join(a.data, '*.json'))]}")
    print(f"Pinecone {read_version()}: http://{a.bind}:{a.port}/")
    if a.bind not in ("127.0.0.1", "localhost", "::1"):
        print(f"WARNING: listening on {a.bind}. No authentication. Anyone who can reach this port sees every track.")
    print(
        "Serving. Open that address in a browser (through your ssh tunnel if the box is remote). Page requests are logged here. Ctrl-C stops it."
    )
    sys.stdout.flush()
    try:
        ThreadingHTTPServer((a.bind, a.port), H).serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
