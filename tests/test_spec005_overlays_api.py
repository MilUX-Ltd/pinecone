"""Spec 005, criteria 2 and 3, from the outside: an imported pack reaches the player, its overlays
are listed, and each can be turned off."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

MANIFEST = """<?xml version="1.0" encoding="UTF-8"?>
<MissionPackageManifest version="2">
  <Configuration>
    <Parameter name="uid" value="ex-cedar"/>
    <Parameter name="name" value="EX CEDAR (synthetic)"/>
  </Configuration>
  <Contents>{entries}</Contents>
</MissionPackageManifest>
"""

BOUNDARY = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document><name>Boundary</name>
  <Placemark><name>AO CEDAR</name>
    <Polygon><outerBoundaryIs><LinearRing><coordinates>
      -1.499,51.204,0 -1.475,51.204,0 -1.475,51.216,0 -1.499,51.216,0 -1.499,51.204,0
    </coordinates></LinearRing></outerBoundaryIs></Polygon>
  </Placemark>
</Document></kml>
"""

PHASE = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document><name>Phase lines</name>
  <Placemark><name>PL GRANITE</name>
    <TimeSpan><begin>2026-09-03T07:00:00Z</begin><end>2026-09-03T09:30:00Z</end></TimeSpan>
    <LineString><coordinates>-1.499,51.210,0 -1.475,51.210,0</coordinates></LineString>
  </Placemark>
</Document></kml>
"""

REPORTED = """<?xml version="1.0"?>
<event version="2.0" uid="RPT-1" type="a-h-G" how="h-e"
       time="2026-09-03T08:10:00Z" start="2026-09-03T08:10:00Z" stale="2026-09-03T10:10:00Z">
  <point lat="51.2075" lon="-1.4905" hae="88" ce="250" le="9999999"/>
  <detail><contact callsign="REPORTED CONTACT"/><remarks>Called in, not observed</remarks></detail>
</event>
"""


def get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


@pytest.fixture()
def served(tmp_path: Path):
    files = {"boundary.kml": BOUNDARY, "phase-lines.kml": PHASE, "reported.cot": REPORTED}
    pack = tmp_path / "ex-cedar.zip"
    entries = "".join(f'<Content ignore="false" zipEntry="{p}"/>' for p in files)
    with zipfile.ZipFile(pack, "w") as z:
        z.writestr("MANIFEST/manifest.xml", MANIFEST.format(entries=entries))
        for path, body in files.items():
            z.writestr(path, body)

    data = tmp_path / "data"
    data.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    packs = tmp_path / "packs"
    packs.mkdir()
    port = 9560 + (os.getpid() % 50)
    p = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "serve.py"),
            "--port",
            str(port),
            "--data",
            str(data),
            "--state",
            str(state),
            "--packs",
            str(packs),
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
        yield f"http://127.0.0.1:{port}", pack
    finally:
        p.terminate()
        p.wait(timeout=5)


def test_a_pack_can_be_imported_and_its_overlays_listed(served) -> None:
    """Criterion 1 and 2, from the outside."""
    base, pack = served
    req = urllib.request.Request(
        f"{base}/api/packs/import",
        data=f"path={pack}".encode(),
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        imported = json.load(r)
    assert imported["name"] == "EX CEDAR (synthetic)"
    assert {o["name"] for o in imported["overlays"]} == {"Boundary", "Phase lines", "reported.cot"}

    code, body = get(f"{base}/api/packs")
    assert code == 200
    packs = json.loads(body)
    assert len(packs) == 1
    assert packs[0]["name"] == "EX CEDAR (synthetic)"


def test_the_overlays_come_back_as_shapes_the_player_can_draw(served) -> None:
    """Criterion 2. The player reads shapes through the read API, never a file path."""
    base, pack = served
    urllib.request.urlopen(
        urllib.request.Request(f"{base}/api/packs/import", data=f"path={pack}".encode(), method="POST"),
        timeout=10,
    ).read()
    code, body = get(f"{base}/api/packs/ex-cedar/overlays")
    assert code == 200
    overlays = json.loads(body)
    shapes = [s for o in overlays for s in o["shapes"]]
    kinds = {s["kind"] for s in shapes}
    assert "polygon" in kinds and "line" in kinds and "point" in kinds
    for s in shapes:
        assert s["coordinates"], "every shape carries its own coordinates"


def test_a_reported_contact_is_labelled_reported_wherever_it_appears(served) -> None:
    """Criterion 5. Red force in a pack is what somebody believed, not where anything was."""
    base, pack = served
    urllib.request.urlopen(
        urllib.request.Request(f"{base}/api/packs/import", data=f"path={pack}".encode(), method="POST"),
        timeout=10,
    ).read()
    code, body = get(f"{base}/api/packs/ex-cedar/overlays")
    shapes = [s for o in json.loads(body) for s in o["shapes"]]
    reported = [s for s in shapes if s.get("reported")]
    assert len(reported) == 1
    assert "reported" in reported[0]["label"].lower()
    assert reported[0]["ce"] == 250.0, "and its reported accuracy is carried, not hidden"


def test_an_overlay_carries_the_window_it_declares(served) -> None:
    """Criterion 4. The picture changed during the exercise; a static overlay lies."""
    base, pack = served
    urllib.request.urlopen(
        urllib.request.Request(f"{base}/api/packs/import", data=f"path={pack}".encode(), method="POST"),
        timeout=10,
    ).read()
    code, body = get(f"{base}/api/packs/ex-cedar/overlays")
    shapes = {s["label"]: s for o in json.loads(body) for s in o["shapes"]}
    granite = shapes["PL GRANITE"]
    assert granite["begin_ms"] and granite["end_ms"] and granite["begin_ms"] < granite["end_ms"]
    cedar = shapes["AO CEDAR"]
    assert cedar["begin_ms"] is None and cedar["undated"] is True


def test_a_pack_that_is_not_one_is_refused_with_a_reason(served) -> None:
    base, pack = served
    plain = pack.parent / "plain.zip"
    with zipfile.ZipFile(plain, "w") as z:
        z.writestr("hello.txt", "no manifest here")
    req = urllib.request.Request(f"{base}/api/packs/import", data=f"path={plain}".encode(), method="POST")
    try:
        urllib.request.urlopen(req, timeout=10)
        raise AssertionError("it should have been refused")
    except urllib.error.HTTPError as e:
        assert e.code == 400
        assert b"manifest" in e.read().lower()
