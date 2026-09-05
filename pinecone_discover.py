#!/usr/bin/env python3
"""Discover the TAK Server on this box: what it is, where its database is, what it keeps.

Pure functions over text, so the suite runs anywhere, plus a gatherer that runs on a box.
Nothing here ever returns, prints or stores a credential. The installer runs this as root;
the page reads the JSON it wrote and adds the live facts with Pinecone's own role.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import ipaddress
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable, Iterable
from datetime import datetime
from datetime import timezone as dt_timezone
from typing import Any

TAK_PORTS = (8089, 8443, 8446, 8444, 9001, 5432)
CONFIG_FILES = ("/opt/tak/CoreConfig.xml", "/opt/tak/CoreConfig.example.xml")
RETENTION_FILE = "/opt/tak/conf/retention/retention-policy.yml"
UNIT = "takserver.service"


# ---------- pure functions ----------
def parse_version(dpkg_out: str, rpm_out: str) -> tuple[str | None, str]:
    """The TAK Server version from the package manager. There is no version.txt on 5.x."""
    m = re.search(r"^takserver\s+(\S+)\s*$", dpkg_out, re.M)
    if m:
        return m.group(1), "dpkg"
    m = re.search(r"^takserver-(\S+?)(?:\.noarch|\.x86_64)?\s*$", rpm_out, re.M)
    if m:
        return m.group(1), "rpm"
    return None, "not found"


def parse_connection(xml: str, source: str) -> dict[str, Any] | None:
    """Host, port, database and username from the <connection> element. Never the password."""
    m = re.search(r"<connection\b([^>]*)>", xml)
    if not m:
        return None
    attrs = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
    url = attrs.get("url", "")
    mm = re.match(r"jdbc:postgresql://([^/:\s]+)(?::(\d+))?/([^?\s\"]+)", url)
    if not mm:
        return None
    return {
        "host": mm.group(1),
        "port": int(mm.group(2) or 5432),
        "database": mm.group(3),
        "username": attrs.get("username"),
        "source": source,
    }


def listening_ports(ss_out: str) -> set[int]:
    ports: set[int] = set()
    for line in ss_out.splitlines():
        f = line.split()
        if len(f) >= 4 and f[0] == "LISTEN":
            m = re.search(r":(\d+)$", f[3])
            if m:
                ports.add(int(m.group(1)))
    return ports


def tak_ports_report(ports: set[int]) -> dict[str, bool]:
    return {str(p): p in ports for p in TAK_PORTS}


def file_findings(
    stats: dict[str, tuple[str, str, str]], carries_password: set[str] | None = None
) -> dict[str, dict[str, Any]]:
    """Mode and owner per config file, with the finding a stock install deserves. A file is said to
    carry a password only when the caller saw a non-empty password attribute in it."""
    out: dict[str, dict[str, Any]] = {}
    carries = carries_password if carries_password is not None else set()  # never derived; the caller brings evidence
    for path, (mode, owner, group) in stats.items():
        world = int(mode[-1]) & 4 != 0
        finding = None
        if world and path.endswith("CoreConfig.example.xml"):
            finding = "world-readable" + (
                " and carries the database password from the post-install script" if path in carries else ""
            )
        elif world and path.endswith("CoreConfig.xml"):
            finding = "world-readable: the live server configuration is readable by every local user"
        out[path] = {"mode": mode, "owner": f"{owner}:{group}", "finding": finding}
    return out


def parse_retention(yml: str) -> dict[str, Any]:
    ttls: dict[str, int | None] = {}
    inside = False
    for line in yml.splitlines():
        if re.match(r"^dataRetentionMap:\s*$", line):
            inside = True
            continue
        if inside:
            m = re.match(r"^\s+(\w+):\s*(.*?)\s*$", line)
            if not m:
                inside = False
                continue
            v = m.group(2)
            ttls[m.group(1)] = None if v in ("", "null", "~") else int(v) if v.isdigit() else v  # type: ignore[assignment]
    return {"ttls": ttls, "purges": any(v is not None for v in ttls.values())}


def report(
    *,
    version: tuple[str | None, str],
    unit_state: str,
    ports: dict[str, bool],
    connection: dict[str, Any] | None,
    files: dict[str, dict[str, Any]],
    retention: dict[str, Any],
    retention_source: str,
    timezone: str | None,
    rows: tuple[str, str, str, str] | None,
    credential: dict[str, Any],
    pinecone_version: str = "",
) -> dict[str, Any]:
    cred = dict(credential)
    cred.setdefault(
        "statement", credential_statement(cred.get("role", "pinecone"), cred.get("grant", "SELECT on cot_router"))
    )
    return {
        "pinecone": {"version": pinecone_version},
        "tak": {
            "version": version[0],
            "version_source": version[1],
            "unit": UNIT,
            "unit_state": unit_state,
            "ports": ports,
        },
        "database": {
            **(connection or {"host": None, "port": None, "database": None, "username": None, "source": "not found"}),
            "timezone": timezone,
        },
        "files": files,
        "retention": {**retention, "source": retention_source},
        "rows": {"count": int(rows[0]), "uids": int(rows[1]), "oldest": rows[2], "newest": rows[3]} if rows else None,
        "credential": cred,
        "discovered_at": datetime.now(dt_timezone.utc).isoformat(timespec="seconds"),
    }


def credential_statement(role: str = "pinecone", grant: str = "SELECT on cot_router") -> str:
    return (
        f"Pinecone reads with its own role {role}, {grant} only. "
        "The martiuser credential in CoreConfig.xml was read for nothing and used for nothing."
    )


def render_text(rep: dict[str, Any]) -> str:
    t, d, r = rep["tak"], rep["database"], rep["retention"]
    lines = [
        f"TAK Server {t['version'] or 'version unknown'} (from {t['version_source']}); {t['unit']} is {t['unit_state']}",
        "Listening: " + ", ".join(f"{p} {'yes' if on else 'no'}" for p, on in t["ports"].items()),
        f"Database: {d.get('host')}:{d.get('port')}/{d.get('database')} as {d.get('username')} (from {d.get('source')})",
    ]
    if d.get("timezone"):
        lines.append(f"Database display timezone: {d['timezone']} (Pinecone reads everything as UTC)")
    for path, f in rep["files"].items():
        lines.append(f"{path}: mode {f['mode']} {f['owner']}" + (f", finding: {f['finding']}" if f["finding"] else ""))
    ttls = ", ".join(f"{k}={'null' if v is None else v}" for k, v in r["ttls"].items()) or "no dataRetentionMap"
    lines.append(
        f"Retention ({r['source']}): {ttls}; " + ("something is purged" if r["purges"] else "nothing is purged")
    )
    if rep["rows"]:
        lines.append(
            f"cot_router: {rep['rows']['count']:,} rows, {rep['rows']['uids']:,} uids, {rep['rows']['oldest']} to {rep['rows']['newest']}"
        )
    lines.append(f"Credential: {rep['credential']['statement']}")
    return "\n".join(lines)


# ---------- gathering on a box ----------
def _run(cmd: list[str]) -> str:
    if not shutil.which(cmd[0]):
        return ""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _psql(sql: str, database: str) -> str:
    return _run(["sudo", "-u", "postgres", "psql", "-At", "-d", database, "-c", sql]).strip()


def _stat(path: str) -> tuple[str, str, str] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    import grp
    import pwd

    mode = oct(st.st_mode & 0o777)[2:]
    try:
        owner = pwd.getpwuid(st.st_uid).pw_name
    except KeyError:
        owner = str(st.st_uid)
    try:
        group = grp.getgrgid(st.st_gid).gr_name
    except KeyError:
        group = str(st.st_gid)
    return mode, owner, group


def gather(
    root: str = "/", *, with_db: bool = True, credential: dict[str, Any] | None = None, pinecone_version: str = ""
) -> dict[str, Any]:
    def p(path: str) -> str:
        return os.path.join(root, path.lstrip("/"))

    version = parse_version(
        _run(["dpkg-query", "-W", "-f", "${Package} ${Version}\\n", "takserver"]), _run(["rpm", "-q", "takserver"])
    )
    unit_state = (_run(["systemctl", "is-active", UNIT]).strip() or "unknown").splitlines()[0]
    ports = tak_ports_report(listening_ports(_run(["ss", "-ltn"])))
    connection = None
    for cf in CONFIG_FILES:
        try:
            with open(p(cf), encoding="utf-8", errors="replace") as f:
                connection = parse_connection(f.read(), cf)
        except OSError:
            continue
        if connection:
            break
    stats = {cf: s for cf in CONFIG_FILES if (s := _stat(p(cf)))}
    carries: set[str] = set()
    for cf in stats:
        try:
            with open(p(cf), encoding="utf-8", errors="replace") as f:
                if re.search(r'<connection\b[^>]*\bpassword="[^"]+"', f.read()):
                    carries.add(cf)
        except OSError:
            continue
    files = file_findings(stats, carries)
    try:
        with open(p(RETENTION_FILE), encoding="utf-8") as f:
            retention = parse_retention(f.read())
    except OSError:
        retention = {"ttls": {}, "purges": False}
    tz: str | None = None
    rows: tuple[str, str, str, str] | None = None
    if with_db and connection:
        db = connection["database"]
        tz = _psql("SHOW timezone;", db) or None
        got = _psql(
            "SELECT count(*), count(DISTINCT uid), to_char(min(servertime) AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS') || 'Z', to_char(max(servertime) AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS') || 'Z' FROM cot_router;",
            db,
        )
        parts = got.split("|")
        if len(parts) == 4 and parts[0].isdigit():
            rows = (parts[0], parts[1], parts[2], parts[3])
    return report(
        version=version,
        unit_state=unit_state,
        ports=ports,
        connection=connection,
        files=files,
        retention=retention,
        retention_source=RETENTION_FILE,
        timezone=tz,
        rows=rows,
        credential=credential or {"role": "pinecone", "grant": "SELECT on cot_router", "created": False},
        pinecone_version=pinecone_version,
    )


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Discover the TAK Server on this box. Prints JSON or text. Never a credential."
    )
    ap.add_argument("--root", default="/", help="filesystem root (a directory standing in for a box, for the suite)")
    ap.add_argument("--text", action="store_true", help="print the report as text instead of JSON")
    ap.add_argument("--no-db", action="store_true", help="skip the database queries")
    ap.add_argument("--pinecone-version", default="")
    ap.add_argument("--credential-json", default="", help="credential facts to merge, as JSON (no secret ever)")
    a = ap.parse_args()
    cred = json.loads(a.credential_json) if a.credential_json else None
    rep = gather(a.root, with_db=not a.no_db, credential=cred, pinecone_version=a.pinecone_version)
    if a.text:
        print(render_text(rep))
    else:
        json.dump(rep, sys.stdout, indent=1)
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())


# ---------- map sources: what this estate already carries (spec 002) ----------
MAP_DIRS = ("/var/lib/pinecone/maps", "/opt/tak-maps")
TILE_PORTS = (8080, 8000, 8081, 9000)
MAX_XML_BYTES = 256 * 1024
MAX_ZIP_ENTRIES = 64
MAX_DEFINITION_BYTES = 8 * 1024 * 1024


def source_id(source: dict[str, Any]) -> str:
    """A stable id for a source, from what it is rather than where it sits in a list."""
    kind = source.get("kind")
    if kind == "mbtiles":
        path = str(source.get("path", ""))
        # The basename alone collides when an estate keeps the same map in two directories.
        digest = hashlib.sha256(os.path.dirname(path).encode()).hexdigest()[:6]
        return f"mbtiles:{os.path.basename(path)}:{digest}"
    if kind == "tile-service":
        return "service:" + str(source.get("service") or slugify(str(source.get("name", ""))))
    return "takmap:" + slugify(str(source.get("name", "")))


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "unnamed"


def is_local_address(host: str) -> bool:
    """Loopback and RFC 1918 only, and only as literal addresses.

    A name is not an address: `127.0.0.1.evil.example` starts with `127.` and resolves wherever its
    owner points it, so only `localhost` itself and literal IPs are accepted.
    """
    h = host.strip("[]")
    if h in ("localhost", "::1"):
        return True
    try:
        # ipaddress rejects what str.isdigit() waves through: '١٢٧.0.0.1' is a name, not an address.
        addr = ipaddress.ip_address(h)
    except ValueError:
        return False
    return bool(addr.is_loopback or addr.is_private)


def _bounds(value: Any) -> list[float] | None:
    if isinstance(value, list) and len(value) == 4:
        try:
            return [float(v) for v in value]
        except (TypeError, ValueError):
            return None
    if isinstance(value, str):
        try:
            got = [float(v) for v in value.split(",")]
        except ValueError:
            return None
        return got if len(got) == 4 else None
    return None


def mbtiles_source(path: str, origin: str) -> dict[str, Any] | None:
    """Describe one .mbtiles from its own metadata. A file that does not answer is not a source."""
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            meta = dict(db.execute("SELECT name, value FROM metadata").fetchall())
        finally:
            db.close()
    except (sqlite3.Error, OSError):
        return None
    if not meta:
        return None
    source: dict[str, Any] = {
        "kind": "mbtiles",
        "name": meta.get("name") or os.path.basename(path),
        "format": meta.get("format") or "png",
        "minzoom": int(meta["minzoom"]) if str(meta.get("minzoom", "")).isdigit() else None,
        "maxzoom": int(meta["maxzoom"]) if str(meta.get("maxzoom", "")).isdigit() else None,
        "bounds": _bounds(meta.get("bounds")),
        "attribution": meta.get("attribution") or "",
        "path": path,
        "origin": origin,
    }
    source["id"] = source_id(source)
    return source


def mbtiles_in(directory: str | os.PathLike[str], origin: str) -> list[dict[str, Any]]:
    out = []
    for path in sorted(glob.glob(os.path.join(str(directory), "*.mbtiles"))):
        got = mbtiles_source(path, origin)
        if got:
            out.append(got)
    return out


def parse_map_source_xml(text: str, path: str, origin: str | None = None) -> dict[str, Any] | None:
    """A TAK customMapSource definition. TAK writes {$z}; a browser wants {z}."""
    if "<customMapSource" not in text:
        return None

    def one(tag: str) -> str | None:
        m = re.search(rf"<{tag}>\s*(.*?)\s*</{tag}>", text, re.S)
        return m.group(1) if m else None

    url = one("url")
    if not url:
        return None
    minz, maxz = one("minZoom") or "", one("maxZoom") or ""
    source: dict[str, Any] = {
        "kind": "tak-map-source",
        "name": one("name") or os.path.basename(path),
        "format": (one("tileType") or "png").lower(),
        "minzoom": int(minz) if minz.isdigit() else None,
        "maxzoom": int(maxz) if maxz.isdigit() else None,
        "bounds": None,
        "attribution": "",
        "url_template": url.replace("{$", "{"),
        "path": path,
        "origin": origin or f"a map-source definition on this box: {path}",
    }
    source["id"] = source_id(source)
    return source


def map_sources_in_zip(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """The customMapSource definitions inside a data package, read defensively."""
    out: list[dict[str, Any]] = []
    try:
        with zipfile.ZipFile(path) as zf:
            read = 0
            for info in zf.infolist():
                if read >= MAX_ZIP_ENTRIES:
                    break
                if not info.filename.lower().endswith(".xml") or info.file_size > MAX_XML_BYTES:
                    continue
                read += 1
                try:
                    text = zf.read(info).decode("utf-8", "replace")
                except (OSError, zipfile.BadZipFile):
                    continue
                got = parse_map_source_xml(text, info.filename, origin=f"a data package this estate hands out: {path}")
                if got:
                    out.append(got)
    except (OSError, zipfile.BadZipFile):
        return []
    return out


def map_definitions_in(directory: str | os.PathLike[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    d = str(directory)
    for path in sorted(glob.glob(os.path.join(d, "*.xml"))):
        try:
            if os.path.getsize(path) > MAX_DEFINITION_BYTES:
                continue
            with open(path, encoding="utf-8", errors="replace") as f:
                got = parse_map_source_xml(f.read(), path)
        except OSError:
            continue
        if got:
            out.append(got)
    for path in sorted(glob.glob(os.path.join(d, "*.zip"))):
        out.extend(map_sources_in_zip(path))
    return out


def tile_services(
    fetch: Callable[[str], str | None], probes: Iterable[tuple[str, int]]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Ask a tile server on this box what it serves. Local addresses only, and never guessed."""
    found: list[dict[str, Any]] = []
    notes: list[str] = []
    for host, port in probes:
        if not is_local_address(host):
            notes.append(f"not asked, because {host} is not on this box or its own network")
            continue
        base = f"http://{host}:{port}"
        listing = fetch(f"{base}/services")
        if not listing:
            notes.append(f"nothing answered /services at {host}:{port}")
            continue
        try:
            services = json.loads(listing)
        except ValueError:
            notes.append(f"{base}/services answered with something that is not JSON")
            continue
        if not isinstance(services, list):
            continue
        for entry in services:
            if not isinstance(entry, dict) or not entry.get("url"):
                continue
            url = str(entry["url"])
            ident = url.rstrip("/").rsplit("/", 1)[-1]
            fmt = str(entry.get("imageType") or "png")
            source: dict[str, Any] = {
                "kind": "tile-service",
                "service": ident,
                "name": str(entry.get("name") or ident),
                "format": fmt,
                "minzoom": None,
                "maxzoom": None,
                "bounds": None,
                "attribution": "",
                "url_template": f"{url}/tiles/{{z}}/{{x}}/{{y}}.{fmt}",
                "origin": f"this box's tile server at {host}:{port}",
            }
            detail = fetch(url)
            if detail:
                try:
                    tj = json.loads(detail)
                except ValueError:
                    tj = {}
                if isinstance(tj, dict):
                    tiles = tj.get("tiles")
                    if isinstance(tiles, list) and tiles:
                        source["url_template"] = str(tiles[0])
                    for key in ("minzoom", "maxzoom"):
                        if isinstance(tj.get(key), int):
                            source[key] = tj[key]
                    source["bounds"] = _bounds(tj.get("bounds")) or None
                    source["attribution"] = str(tj.get("attribution") or "")
                    source["name"] = str(tj.get("name") or source["name"])
                    source["format"] = str(tj.get("format") or fmt)
            source["id"] = source_id(source)
            found.append(source)
    return found, notes


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """A service on this box does not get to send us somewhere else."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        return None


# `build_opener` keeps the default handlers and only replaces the classes it is given, so file:,
# ftp: and https: are all still on this opener. What stops them is the scheme check in http_get,
# not the opener. The empty ProxyHandler is what keeps a request to a private address on this box:
# without it, an http_proxy in the unit's environment would send it somewhere else entirely.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPHandler, _NoRedirects)


def http_get(url: str, timeout: float = 3.0) -> str | None:
    """One short request to a local service: http only, no redirects, loopback or private only."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "http" or not is_local_address(parsed.hostname or ""):
        return None
    try:
        with _OPENER.open(url, timeout=timeout) as r:
            return str(r.read(2 * 1024 * 1024).decode("utf-8", "replace"))
    except Exception:
        return None


def listening_tile_probes(ss_out: str) -> list[tuple[str, int]]:
    ports = listening_ports(ss_out)
    return [("127.0.0.1", p) for p in TILE_PORTS if p in ports]


def find_map_sources(
    dirs: Iterable[str | os.PathLike[str]],
    probes: Iterable[tuple[str, int]],
    fetch: Callable[[str], str | None] | None = None,
) -> dict[str, Any]:
    """Everything this box carries or serves, with where each came from. Nothing invented."""
    fetch = fetch or http_get
    sources: list[dict[str, Any]] = []
    notes: list[str] = []
    seen: set[str] = set()
    for d in dirs:
        p = str(d)
        origin = "the tile server's own directory" if p in MAP_DIRS[1:] else f"a directory on this box: {p}"
        for s in mbtiles_in(p, origin) + map_definitions_in(p):
            if s["id"] not in seen:
                seen.add(s["id"])
                sources.append(s)
    services, service_notes = tile_services(fetch, probes)
    notes.extend(service_notes)
    # A box that serves its own mbtiles offers each map twice, as a file and through its tile
    # server. That is one map to an operator. The file wins, because it works with no network at
    # all, and the service is recorded on it as the other way to the same tiles.
    by_name = {str(s["name"]): s for s in sources if s["kind"] == "mbtiles"}
    for s in services:
        twin = by_name.get(str(s["name"]))
        if twin is not None:
            twin["also_served_by"] = s["url_template"]
            twin["origin"] = f"{twin['origin']}, and served by this box's tile server"
            continue
        if s["id"] not in seen:
            seen.add(s["id"])
            sources.append(s)
    return {"sources": sources, "notes": notes}
