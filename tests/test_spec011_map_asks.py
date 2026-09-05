"""Spec 011, the map asks, never guesses: nothing is chosen for you, the online map is a source the
browser uses, and a pack is drawn when you choose it. Everything here is synthetic."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def make_mbtiles(path: Path, name: str) -> None:
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE metadata (name text, value text);"
        "CREATE TABLE tiles (zoom_level integer, tile_column integer, tile_row integer, tile_data blob);"
    )
    db.executemany(
        "INSERT INTO metadata VALUES (?, ?)",
        [("name", name), ("format", "png"), ("minzoom", "8"), ("maxzoom", "16"), ("attribution", "Crown copyright")],
    )
    db.execute("INSERT INTO tiles VALUES (16, 10, ?, ?)", ((1 << 16) - 1 - 20, b"PNGBYTES"))
    db.commit()
    db.close()


def call(url: str, data: dict[str, str] | None = None) -> tuple[int, str]:
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def start(tmp_path: Path, extra: list[str] | None = None) -> tuple[subprocess.Popen, str, Path]:
    maps = tmp_path / "maps"
    maps.mkdir(exist_ok=True)
    make_mbtiles(maps / "andover.mbtiles", "Andover and district")
    make_mbtiles(maps / "alpena.mbtiles", "Alpena CRTC")
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "state").mkdir(exist_ok=True)
    port = 9900 + (os.getpid() % 50)  # a band of its own; the reviewers use 9950 and up
    argv = [
        sys.executable,
        str(ROOT / "serve.py"),
        "--port",
        str(port),
        "--data",
        str(tmp_path / "data"),
        "--maps",
        str(maps),
        "--state",
        str(tmp_path / "state"),
    ]
    p = subprocess.Popen(argv + (extra or []), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(60):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/version", timeout=1).read()
            break
        except Exception:
            time.sleep(0.1)
    return p, f"http://127.0.0.1:{port}", maps


@pytest.fixture()
def box(tmp_path: Path):
    p, base, maps = start(tmp_path)
    try:
        yield base, maps
    finally:
        p.terminate()
        p.wait(timeout=5)


# ---- criterion 1 ----------------------------------------------------------------------------------------


def test_nothing_is_chosen_by_default(box) -> None:
    base, _ = box
    meta = json.loads(call(f"{base}/tiles/meta")[1])
    assert meta["available"] is False and meta["chosen"] is None and not meta.get("url")
    maps = json.loads(call(f"{base}/api/maps")[1])
    assert maps["chosen"] is None
    ids = [s["id"] for s in maps["sources"]]
    assert "online:osm" in ids and any(i.startswith("mbtiles:") for i in ids)
    osm = next(s for s in maps["sources"] if s["id"] == "online:osm")
    assert osm["kind"] == "online" and "openstreetmap.org" in osm["url_template"]
    assert "browser" in osm["origin"] and osm["needs_network"] is True
    assert "OpenStreetMap" in osm["attribution"]
    assert ids[0] == "online:osm", "the online map is listed first"


# ---- criterion 2 ----------------------------------------------------------------------------------------


def test_choosing_the_online_map_hands_the_browser_the_template(box) -> None:
    base, _ = box
    code, body = call(f"{base}/api/maps/choose", {"id": "online:osm"})
    assert code == 200, body
    meta = json.loads(call(f"{base}/tiles/meta")[1])
    assert (
        meta["chosen"] == "online:osm" and "openstreetmap.org" in meta["url"] and "OpenStreetMap" in meta["attribution"]
    )


def test_the_box_never_proxies_the_online_map(box) -> None:
    base, _ = box
    call(f"{base}/api/maps/choose", {"id": "online:osm"})
    code, _ = call(f"{base}/tiles/10/512/340.png")
    assert code == 404, "the box has no tile of its own to give, and fetches none"


# ---- criterion 3 ----------------------------------------------------------------------------------------


def test_choosing_a_pack_draws_the_pack(box) -> None:
    base, _ = box
    maps = json.loads(call(f"{base}/api/maps")[1])
    pack = next(s for s in maps["sources"] if s["name"] == "Alpena CRTC")
    assert call(f"{base}/api/maps/choose", {"id": pack["id"]})[0] == 200
    meta = json.loads(call(f"{base}/tiles/meta")[1])
    assert meta["available"] is True and meta["chosen"] == pack["id"] and meta["meta"]["name"] == "Alpena CRTC"
    code, body = call(f"{base}/tiles/16/10/20.png")
    assert code == 200 and body == "PNGBYTES"


def test_a_file_named_on_the_command_line_still_wins(tmp_path: Path) -> None:
    named = tmp_path / "named.mbtiles"
    make_mbtiles(named, "Named on the command line")
    p, base, _ = start(tmp_path, ["--tiles", str(named)])
    try:
        meta = json.loads(call(f"{base}/tiles/meta")[1])
        assert meta["available"] is True and meta["meta"]["name"] == "Named on the command line"
    finally:
        p.terminate()
        p.wait(timeout=5)
