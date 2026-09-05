"""Spec 002, criterion 4 and the escaping the threat note requires: the sources are shown with
their origin, one can be chosen, and the choice is what the map then uses."""

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


@pytest.fixture()
def page(tmp_path: Path):
    maps = tmp_path / "maps"
    maps.mkdir()
    make_mbtiles(maps / "andover.mbtiles", "Andover and district")
    make_mbtiles(maps / "hostile.mbtiles", "<script>alert(1)</script>")
    data = tmp_path / "data"
    data.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    port = 8990 + (os.getpid() % 90)
    p = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "serve.py"),
            "--port",
            str(port),
            "--data",
            str(data),
            "--maps",
            str(maps),
            "--state",
            str(state),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/version", timeout=1).read()
                break
            except Exception:
                time.sleep(0.1)
        yield f"http://127.0.0.1:{port}", state
    finally:
        p.terminate()
        p.wait(timeout=5)


def get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def post(url: str, fields: dict[str, str]) -> tuple[int, str]:
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_the_page_says_where_each_source_came_from(page) -> None:
    base, _ = page
    code, body = get(f"{base}/api/maps")
    assert code == 200
    d = json.loads(body)
    names = {s["name"]: s for s in d["sources"]}
    assert "Andover and district" in names
    assert names["Andover and district"]["origin"]
    assert names["Andover and district"]["kind"] == "mbtiles"
    code, html = get(f"{base}/status")
    assert code == 200
    assert "Andover and district" in html
    assert "Map" in html or "map" in html


def test_a_hostile_name_is_escaped(page) -> None:
    base, _ = page
    code, html = get(f"{base}/status")
    assert code == 200
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_choosing_a_source_changes_what_the_map_uses(page) -> None:
    base, state = page
    d = json.loads(get(f"{base}/api/maps")[1])
    andover = next(s for s in d["sources"] if s["name"] == "Andover and district")
    hostile = next(s for s in d["sources"] if s["name"].startswith("<script"))
    code, _ = post(f"{base}/api/maps/choose", {"id": andover["id"]})
    assert code == 200
    meta = json.loads(get(f"{base}/tiles/meta")[1])
    assert meta["available"] is True
    assert meta["meta"]["name"] == "Andover and district"
    assert get(f"{base}/tiles/16/10/20.png") == (200, "PNGBYTES")
    assert json.loads(get(f"{base}/api/maps")[1])["chosen"] == andover["id"]
    code, _ = post(f"{base}/api/maps/choose", {"id": hostile["id"]})
    assert code == 200
    meta = json.loads(get(f"{base}/tiles/meta")[1])
    assert meta["meta"]["name"] == "<script>alert(1)</script>", "the JSON carries the raw name; the page escapes it"
    assert (state / "map-choice.json").exists(), "the choice outlives the request"


def test_only_a_discovered_id_can_be_chosen(page) -> None:
    base, _ = page
    code, body = post(f"{base}/api/maps/choose", {"id": "mbtiles:/etc/passwd"})
    assert code == 400
    assert "not one of the sources" in body.lower() or "unknown" in body.lower()
    code, body = post(f"{base}/api/maps/choose", {"id": ""})
    assert code == 400


def test_a_url_template_source_is_handed_to_the_browser(tmp_path: Path) -> None:
    """A chosen source that is a URL template is described as one, and Pinecone does not proxy it."""
    state = tmp_path / "state"
    state.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    maps = tmp_path / "maps"
    maps.mkdir()
    (maps / "andover.xml").write_text(
        "<customMapSource><name>Estate tiles</name><minZoom>8</minZoom><maxZoom>16</maxZoom>"
        "<tileType>png</tileType><url>http://192.168.88.10:8080/services/andover/tiles/{$z}/{$x}/{$y}.png</url></customMapSource>"
    )
    port = 9090 + (os.getpid() % 80)
    p = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "serve.py"),
            "--port",
            str(port),
            "--data",
            str(data),
            "--maps",
            str(maps),
            "--state",
            str(state),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/version", timeout=1).read()
                break
            except Exception:
                time.sleep(0.1)
        base = f"http://127.0.0.1:{port}"
        d = json.loads(get(f"{base}/api/maps")[1])
        est = next(s for s in d["sources"] if s["name"] == "Estate tiles")
        assert post(f"{base}/api/maps/choose", {"id": est["id"]})[0] == 200
        meta = json.loads(get(f"{base}/tiles/meta")[1])
        assert meta["url"] == "http://192.168.88.10:8080/services/andover/tiles/{z}/{x}/{y}.png"
        html = get(f"{base}/status")[1]
        assert "192.168.88.10" in html, "the page shows where the browser will fetch tiles from"
    finally:
        p.terminate()
        p.wait(timeout=5)
