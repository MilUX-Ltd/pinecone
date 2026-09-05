"""serve.py: what it serves, from where, and what it refuses."""

from __future__ import annotations

import json
import sqlite3
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest


def make_mbtiles(path: Path) -> None:
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE metadata (name text, value text);"
        "CREATE TABLE tiles (zoom_level integer, tile_column integer, tile_row integer, tile_data blob);"
    )
    db.executemany(
        "INSERT INTO metadata VALUES (?, ?)",
        [("name", "Test tiles"), ("format", "png"), ("minzoom", "8"), ("maxzoom", "16")],
    )
    # XYZ (z=16, x=10, y=20) is stored TMS as row 2**16 - 1 - 20
    db.execute("INSERT INTO tiles VALUES (16, 10, ?, ?)", ((1 << 16) - 1 - 20, b"PNGBYTES"))
    db.commit()
    db.close()


@pytest.fixture()
def server(serve, tmp_path: Path):
    tiles = tmp_path / "t.mbtiles"
    make_mbtiles(tiles)
    data = tmp_path / "data"
    data.mkdir()
    (data / "one.json").write_text(
        json.dumps({"format": "pinecone-bundle/0", "window": {"start": 1, "end": 2}, "tracks": []})
    )
    (data / "notes.csv").write_text("id,uid\n")
    serve.H.tiles = serve.Tiles(str(tiles))
    serve.H.data_dir = str(data)
    serve.H.tiles_url = None
    serve.H.attribution = None
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), serve.H)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def get(url: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_tiles_come_out_of_the_mbtiles_with_the_row_flipped(server: str) -> None:
    assert get(f"{server}/tiles/16/10/20.png") == (200, b"PNGBYTES")
    assert get(f"{server}/tiles/16/10/21.png")[0] == 404
    meta = json.loads(get(f"{server}/tiles/meta")[1])
    assert meta["available"] is True and meta["meta"]["name"] == "Test tiles"


def test_bundles_lists_only_json_and_serves_by_name(server: str) -> None:
    names = [b["name"] for b in json.loads(get(f"{server}/bundles")[1])]
    assert names == ["one"]
    assert json.loads(get(f"{server}/bundle.json?name=one")[1])["format"] == "pinecone-bundle/0"
    assert get(f"{server}/bundle.json?name=../pyproject")[0] == 404, "a path is reduced to its basename"
    assert get(f"{server}/bundle.json")[0] == 404


def test_static_is_served_by_basename_only(server: str) -> None:
    assert get(f"{server}/")[0] == 200
    assert get(f"{server}/vendor/leaflet.js")[0] == 200
    assert get(f"{server}/static/../CLAUDE.md")[0] == 404
    assert get(f"{server}/favicon.ico")[0] == 204
    assert get(f"{server}/nothing-here")[0] == 404


def test_version_reports_the_version_file(server: str, root: Path) -> None:
    v = json.loads(get(f"{server}/version")[1])
    assert v["version"] == (root / "VERSION").read_text().strip()
    assert v["local"] is True


def test_update_apply_refuses_anything_but_loopback(serve) -> None:
    # The handler checks the client address before doing anything; a non-loopback client gets 403.
    class Fake(serve.H):
        def __init__(self) -> None:
            self.client_address = ("10.0.0.9", 1234)
            self.path = "/update/apply"
            self.sent: list[int] = []

        def send(self, code, body=b"", ctype="text/plain"):
            self.sent.append(code)

    f = Fake()
    f.do_POST()
    assert f.sent == [403]
