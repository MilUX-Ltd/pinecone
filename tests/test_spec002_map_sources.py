"""Spec 002, criteria 1, 2, 3 and 5: what the estate already carries is found, described, and
never invented. The shapes here are the ones a real tile server and real data packages return.
"""

from __future__ import annotations

import json
import sqlite3
import zipfile
from pathlib import Path

from conftest import load

# The real shape of a customMapSource XML from a map-source data package.
MAP_SOURCE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<customMapSource>
  <name>Andover and district</name>
  <minZoom>8</minZoom>
  <maxZoom>16</maxZoom>
  <tileType>png</tileType>
  <tileUpdate>None</tileUpdate>
  <url>http://192.168.88.10:8080/services/andover/tiles/{$z}/{$x}/{$y}.png</url>
  <backgroundColor>#000000</backgroundColor>
</customMapSource>
"""
# The real shape of mbtileserver's listing and of one service's TileJSON.
SERVICES_JSON = json.dumps(
    [
        {
            "imageType": "png",
            "url": "http://127.0.0.1:8080/services/andover",
            "name": "Andover and district (OS VectorMap District)",
        },
        {"imageType": "jpg", "url": "http://127.0.0.1:8080/services/wpafb", "name": "Wright-Patterson AFB"},
    ]
)
TILEJSON = json.dumps(
    {
        "attribution": "Contains OS data © Crown copyright and database right 2026.",
        "bounds": [-1.85, 51.02, -1.08, 51.30],
        "format": "png",
        "maxzoom": 16,
        "minzoom": 8,
        "name": "Andover and district (OS VectorMap District)",
        "scheme": "xyz",
        "tiles": ["http://127.0.0.1:8080/services/andover/tiles/{z}/{x}/{y}.png"],
        "tilejson": "2.1.0",
    }
)


def make_mbtiles(path: Path, name: str = "Test area", fmt: str = "png") -> None:
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE metadata (name text, value text);"
        "CREATE TABLE tiles (zoom_level integer, tile_column integer, tile_row integer, tile_data blob);"
    )
    db.executemany(
        "INSERT INTO metadata VALUES (?, ?)",
        [
            ("name", name),
            ("format", fmt),
            ("minzoom", "8"),
            ("maxzoom", "16"),
            ("bounds", "-1.85,51.02,-1.08,51.30"),
            ("attribution", "Crown copyright"),
        ],
    )
    db.execute("INSERT INTO tiles VALUES (16, 10, 20, ?)", (b"PNG",))
    db.commit()
    db.close()


def test_mbtiles_on_disk_are_found_and_described(tmp_path: Path) -> None:
    d = load("pinecone_discover")
    a = tmp_path / "opt-tak-maps"
    a.mkdir()
    make_mbtiles(a / "andover.mbtiles", "Andover and district")
    make_mbtiles(a / "wpafb.mbtiles", "Wright-Patterson AFB", fmt="jpg")
    (a / "notes.txt").write_text("not a map")
    found = d.mbtiles_in(a, origin="the tile server's own directory")
    by = {s["name"]: s for s in found}
    assert set(by) == {"Andover and district", "Wright-Patterson AFB"}
    one = by["Andover and district"]
    assert one["kind"] == "mbtiles"
    assert one["minzoom"] == 8 and one["maxzoom"] == 16
    assert one["format"] == "png" and one["attribution"] == "Crown copyright"
    assert one["bounds"] == [-1.85, 51.02, -1.08, 51.30]
    assert one["path"] == str(a / "andover.mbtiles")
    assert one["origin"] == "the tile server's own directory"
    assert one["id"] and one["id"] == d.source_id(one)


def test_a_broken_mbtiles_is_skipped_with_its_reason(tmp_path: Path) -> None:
    d = load("pinecone_discover")
    (tmp_path / "broken.mbtiles").write_text("this is not sqlite")
    found = d.mbtiles_in(tmp_path, origin="x")
    assert found == []


def test_a_tile_server_is_asked_what_it_serves() -> None:
    d = load("pinecone_discover")
    asked: list[str] = []

    def fetch(url: str) -> str | None:
        asked.append(url)
        if url.endswith("/services"):
            return SERVICES_JSON
        if url.endswith("/services/andover"):
            return TILEJSON
        return None

    found, notes = d.tile_services(fetch, [("127.0.0.1", 8080)])
    assert "http://127.0.0.1:8080/services" in asked
    by = {s["name"]: s for s in found}
    assert "Andover and district (OS VectorMap District)" in by
    one = by["Andover and district (OS VectorMap District)"]
    assert one["kind"] == "tile-service"
    assert one["url_template"] == "http://127.0.0.1:8080/services/andover/tiles/{z}/{x}/{y}.png"
    assert one["minzoom"] == 8 and one["maxzoom"] == 16
    assert one["attribution"].startswith("Contains OS data")
    assert "127.0.0.1:8080" in one["origin"]
    # the service whose detail did not answer is still listed, from the listing alone
    other = by["Wright-Patterson AFB"]
    assert other["url_template"] == "http://127.0.0.1:8080/services/wpafb/tiles/{z}/{x}/{y}.jpg"
    assert other["minzoom"] is None
    assert notes == [] or all(isinstance(n, str) for n in notes)


def test_only_local_addresses_are_asked() -> None:
    d = load("pinecone_discover")
    for host in ("127.0.0.1", "::1", "10.1.2.3", "172.16.4.5", "192.168.88.10"):
        assert d.is_local_address(host), host
    for host in ("8.8.8.8", "93.184.216.34", "tile.openstreetmap.org", "172.32.0.1"):
        assert not d.is_local_address(host), host

    asked: list[str] = []

    def fetch(url: str) -> str | None:
        asked.append(url)
        return None

    found, notes = d.tile_services(fetch, [("127.0.0.1", 8080), ("8.8.8.8", 80)])
    assert all("8.8.8.8" not in u for u in asked), "a public address is never asked"
    assert found == []
    assert any("8.8.8.8" in n for n in notes), "and the refusal is recorded"


def test_tak_map_source_definitions_are_found(tmp_path: Path) -> None:
    d = load("pinecone_discover")
    f = tmp_path / "andover.xml"
    f.write_text(MAP_SOURCE_XML)
    got = d.parse_map_source_xml(MAP_SOURCE_XML, str(f))
    assert got is not None
    assert got["kind"] == "tak-map-source"
    assert got["name"] == "Andover and district"
    assert got["minzoom"] == 8 and got["maxzoom"] == 16
    assert got["format"] == "png"
    assert (
        got["url_template"] == "http://192.168.88.10:8080/services/andover/tiles/{z}/{x}/{y}.png"
    ), "TAK's {$z} placeholders are normalised for a browser"
    assert got["path"] == str(f)
    assert d.parse_map_source_xml("<notAMapSource/>", "x") is None


def test_definitions_inside_a_data_package_are_found(tmp_path: Path) -> None:
    d = load("pinecone_discover")
    z = tmp_path / "milux-mapsources-deployed-dp.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("andover.xml", MAP_SOURCE_XML)
        zf.writestr("MANIFEST/manifest.xml", "<MissionPackageManifest version='2'/>")
        zf.writestr("readme.txt", "ignored")
    found = d.map_sources_in_zip(z)
    assert [s["name"] for s in found] == ["Andover and district"]
    assert str(z) in found[0]["origin"]
    assert found[0]["path"].endswith("andover.xml")


def test_nothing_is_listed_that_was_not_read(tmp_path: Path) -> None:
    d = load("pinecone_discover")
    empty = tmp_path / "nowhere"
    empty.mkdir()

    def fetch(url: str) -> str | None:
        return None

    result = d.find_map_sources(dirs=[empty], probes=[], fetch=fetch)
    assert result["sources"] == []
    assert isinstance(result["notes"], list)


def test_every_source_carries_an_id_that_survives_a_round_trip(tmp_path: Path) -> None:
    d = load("pinecone_discover")
    make_mbtiles(tmp_path / "andover.mbtiles")
    (tmp_path / "andover.xml").write_text(MAP_SOURCE_XML)

    def fetch(url: str) -> str | None:
        return SERVICES_JSON if url.endswith("/services") else None

    result = d.find_map_sources(dirs=[tmp_path], probes=[("127.0.0.1", 8080)], fetch=fetch)
    ids = [s["id"] for s in result["sources"]]
    assert len(ids) == len(set(ids)), "ids are unique"
    assert all(ids), "every source has one"
    kinds = {s["kind"] for s in result["sources"]}
    assert kinds == {"mbtiles", "tak-map-source", "tile-service"}
