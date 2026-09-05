"""Spec 005, criteria 1, 4, 5 and 6: a data package is read for what it carries, validity is
honoured, reported is never truth, and a hostile package cannot be made to read the box.

Every package here is built in the test. No real exercise material goes near it, and none could:
real exercise recordings never enter an agent's context."""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MANIFEST = """<?xml version="1.0" encoding="UTF-8"?>
<MissionPackageManifest version="2">
  <Configuration>
    <Parameter name="uid" value="test-pack"/>
    <Parameter name="name" value="{name}"/>
  </Configuration>
  <Contents>{entries}</Contents>
</MissionPackageManifest>
"""

KML = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document><name>{doc}</name>
  <Placemark><name>{pm}</name>{when}
    <LineString><coordinates>-1.49,51.21,0 -1.47,51.21,0</coordinates></LineString>
  </Placemark>
</Document></kml>
"""

COT = """<?xml version="1.0"?>
<event version="2.0" uid="{uid}" type="{typ}" how="{how}"
       time="2026-09-03T08:00:00Z" start="2026-09-03T08:00:00Z" stale="2026-09-03T10:00:00Z">
  <point lat="51.21" lon="-1.48" hae="90" ce="{ce}" le="9999999"/>
  <detail><contact callsign="{cs}"/></detail>
</event>
"""


def pack(tmp: Path, files: dict[str, str], name: str = "TEST PACK") -> Path:
    entries = "".join(f'<Content ignore="false" zipEntry="{p}"/>' for p in files)
    p = tmp / "pack.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("MANIFEST/manifest.xml", MANIFEST.format(name=name, entries=entries))
        for path, body in files.items():
            z.writestr(path, body)
    return p


# ---- criterion 1: read for what it carries -------------------------------------------------


def test_the_overlays_in_a_package_are_found_and_named(tmp_path: Path) -> None:
    import pinecone_packages

    p = pack(
        tmp_path,
        {
            "ground.kml": KML.format(doc="Ground", pm="PL GRANITE", when=""),
            "obj.cot": COT.format(uid="o1", typ="b-m-p-w", how="h-g-i-g-o", ce="10", cs="OBJ CEDAR"),
            "notes.txt": "ignored, but listed",
        },
    )
    got = pinecone_packages.read_package(str(p), str(tmp_path / "out"))
    assert got.name == "TEST PACK"
    kinds = {o.path: o.kind for o in got.overlays}
    assert kinds == {"ground.kml": "kml", "obj.cot": "cot"}
    assert "notes.txt" in got.ignored


def test_a_zip_that_is_not_a_data_package_is_refused(tmp_path: Path) -> None:
    import pinecone_packages

    p = tmp_path / "plain.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("hello.txt", "no manifest here")
    with pytest.raises(pinecone_packages.NotAPackage):
        pinecone_packages.read_package(str(p), str(tmp_path / "out"))


def test_a_manifest_naming_a_file_the_zip_does_not_hold_is_refused(tmp_path: Path) -> None:
    import pinecone_packages

    p = tmp_path / "lying.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr(
            "MANIFEST/manifest.xml",
            MANIFEST.format(name="LIAR", entries='<Content ignore="false" zipEntry="absent.kml"/>'),
        )
    with pytest.raises(pinecone_packages.BadPackage) as e:
        pinecone_packages.read_package(str(p), str(tmp_path / "out"))
    assert "absent.kml" in str(e.value)


# ---- criterion 4: validity windows ---------------------------------------------------------


def test_an_overlay_carries_the_window_it_declares(tmp_path: Path) -> None:
    import pinecone_packages

    when = "<TimeSpan><begin>2026-09-03T07:00:00Z</begin><end>2026-09-03T09:00:00Z</end></TimeSpan>"
    p = pack(tmp_path, {"pl.kml": KML.format(doc="Phase", pm="PL GRANITE", when=when)})
    got = pinecone_packages.read_package(str(p), str(tmp_path / "out"))
    item = got.overlays[0].shapes[0]
    assert item.begin_ms is not None and item.end_ms is not None
    assert item.begin_ms < item.end_ms


def test_an_overlay_with_no_window_says_so_rather_than_inventing_one(tmp_path: Path) -> None:
    import pinecone_packages

    p = pack(tmp_path, {"b.kml": KML.format(doc="Boundary", pm="AO CEDAR", when="")})
    got = pinecone_packages.read_package(str(p), str(tmp_path / "out"))
    item = got.overlays[0].shapes[0]
    assert item.begin_ms is None and item.end_ms is None
    assert item.undated is True


# ---- criterion 5: reported is never truth --------------------------------------------------


def test_a_reported_contact_is_marked_reported(tmp_path: Path) -> None:
    import pinecone_packages

    p = pack(tmp_path, {"r.cot": COT.format(uid="r1", typ="a-h-G", how="h-e", ce="250", cs="REPORTED CONTACT")})
    got = pinecone_packages.read_package(str(p), str(tmp_path / "out"))
    item = got.overlays[0].shapes[0]
    assert item.reported is True


def test_an_observed_item_is_not_marked_reported(tmp_path: Path) -> None:
    import pinecone_packages

    p = pack(tmp_path, {"o.cot": COT.format(uid="o1", typ="b-m-p-w", how="h-g-i-g-o", ce="10", cs="OBJ CEDAR")})
    got = pinecone_packages.read_package(str(p), str(tmp_path / "out"))
    assert got.overlays[0].shapes[0].reported is False


# ---- criterion 6: it cannot be made to read the box ----------------------------------------


@pytest.mark.parametrize("entry", ["../escaped.kml", "/etc/escaped.kml", "a/../../escaped.kml"])
def test_an_entry_escaping_the_extraction_directory_is_refused(tmp_path: Path, entry: str) -> None:
    import pinecone_packages

    p = tmp_path / "evil.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr(
            "MANIFEST/manifest.xml",
            MANIFEST.format(name="EVIL", entries=f'<Content ignore="false" zipEntry="{entry}"/>'),
        )
        z.writestr(entry, KML.format(doc="x", pm="x", when=""))
    out = tmp_path / "out"
    with pytest.raises(pinecone_packages.BadPackage) as e:
        pinecone_packages.read_package(str(p), str(out))
    # Asserting the escaped file does not exist proves nothing: nothing is written to disk on any
    # path. What matters is that the refusal names this rule rather than tripping over some other
    # one by luck, and that it says which entry it stopped.
    assert "outside" in str(e.value).lower()
    assert entry in str(e.value)
    assert not (tmp_path / "escaped.kml").exists()


def test_an_external_entity_in_the_xml_is_refused(tmp_path: Path) -> None:
    import pinecone_packages

    secret = tmp_path / "secret.txt"
    secret.write_text("the password")
    xxe = f"""<?xml version="1.0"?>
<!DOCTYPE kml [<!ENTITY xxe SYSTEM "file://{secret}">]>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document><name>&xxe;</name></Document></kml>
"""
    p = pack(tmp_path, {"x.kml": xxe})
    with pytest.raises(pinecone_packages.BadPackage) as e:
        pinecone_packages.read_package(str(p), str(tmp_path / "out"))
    # This assertion used to sit inside the `raises` block, on the line after the call, so it never
    # ran at all. The refusal must name the rule, and must not carry the thing it was protecting.
    assert "entity" in str(e.value).lower() or "doctype" in str(e.value).lower()
    assert "the password" not in str(e.value)


def test_a_package_that_expands_beyond_the_limit_is_refused(tmp_path: Path) -> None:
    import pinecone_packages

    p = tmp_path / "bomb.zip"
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(
            "MANIFEST/manifest.xml",
            MANIFEST.format(name="BOMB", entries='<Content ignore="false" zipEntry="big.kml"/>'),
        )
        z.writestr("big.kml", "A" * (200 * 1024 * 1024))
    with pytest.raises(pinecone_packages.BadPackage) as e:
        pinecone_packages.read_package(str(p), str(tmp_path / "out"))
    assert "too large" in str(e.value).lower() or "limit" in str(e.value).lower()


# The five findings the pre-UAT review returned, each with the case that would have caught it.


@pytest.mark.parametrize(
    ("cot_type", "how", "expected", "why"),
    [
        ("a-h-G", "h-e", True, "a hostile contact somebody estimated: the case the spec names"),
        ("a-h-G", "m-g", True, "a hostile contact from a machine is still somebody's assertion"),
        ("a-s-G", "h-t", True, "suspect, transcribed"),
        ("a-u-G", "h-g-i-g-o", True, "an unknown contact somebody typed in"),
        ("a-n-G", "h-g-i-g-o", True, "a neutral contact somebody typed in"),
        ("a-f-G", "h-g-i-g-o", True, "even a friendly one, if a human put it there"),
        ("a-f-G", "m-g", False, "a friendly position from GPS is observed"),
        ("b-m-p-w", "h-g-i-g-o", False, "a control measure is a plan, not a claim about anybody"),
    ],
)
def test_what_counts_as_reported(tmp_path: Path, cot_type: str, how: str, expected: bool, why: str) -> None:
    """Criterion 5, the line the product's thesis rests on, as a table rather than two examples.

    The first version of the rule carried `h-g-i-g-o-r`, which is not a CoT value and was a typo
    for the commonest human-entry value there is, so a spot report of an unknown contact was drawn
    as though somebody had watched it happen. Two examples passed; the rule was wrong.
    """
    import pinecone_packages

    p = pack(tmp_path, {"x.cot": COT.format(uid="x", typ=cot_type, how=how, ce="250", cs="X")})
    got = pinecone_packages.read_package(str(p), str(tmp_path / "out"))

    assert got.overlays[0].shapes[0].reported is expected, why


def test_a_window_that_cannot_be_read_is_not_reported_as_no_window(tmp_path: Path) -> None:
    """Criterion 4. An unreadable window used to become no window, and the player then told the
    operator the overlay carried none and applied throughout, which is the opposite of what the
    pack said. KML permits a dateTime with no zone and a plain date, both of which it missed."""
    import pinecone_packages

    unreadable = "<TimeSpan><begin>whenever we set off</begin></TimeSpan>"
    p = pack(tmp_path, {"a.kml": KML.format(doc="D", pm="PL VAGUE", when=unreadable)})
    got = pinecone_packages.read_package(str(p), str(tmp_path / "out"))
    shape = got.overlays[0].shapes[0]

    assert shape.window_unreadable is True
    assert shape.undated is False, "it declared a window; it just could not be read"
    assert shape.begin_ms is None


@pytest.mark.parametrize(
    ("when", "readable"),
    [
        ("<TimeSpan><begin>2026-09-03T07:00:00Z</begin></TimeSpan>", True),
        ("<TimeSpan><begin>2026-09-03T07:00:00</begin></TimeSpan>", True),
        ("<TimeSpan><begin>2026-09-03</begin></TimeSpan>", True),
        ("<TimeSpan><begin>2026-09</begin></TimeSpan>", True),
        ("<TimeSpan><begin>2026</begin></TimeSpan>", True),
        ("<TimeSpan><begin>last Tuesday</begin></TimeSpan>", False),
    ],
)
def test_the_shapes_of_time_kml_actually_allows(tmp_path: Path, when: str, readable: bool) -> None:
    """KML permits all of these, and only the first was read before."""
    import pinecone_packages

    p = pack(tmp_path, {"a.kml": KML.format(doc="D", pm="PL X", when=when)})
    shape = pinecone_packages.read_package(str(p), str(tmp_path / "out")).overlays[0].shapes[0]

    assert (shape.begin_ms is not None) is readable
    assert shape.window_unreadable is not readable


def test_the_packs_own_identity_is_read_not_a_contents(tmp_path: Path) -> None:
    """A real ATAK manifest carries uid and name parameters inside each Content as well as in the
    Configuration. Walking the whole tree let the last of those win, so a real pack was filed under
    a content's uid and shown to the operator under a filename. Every fixture here used the
    self-closing form, so nothing caught it."""
    import pinecone_packages

    manifest = """<?xml version="1.0"?>
<MissionPackageManifest version="2">
  <Configuration>
    <Parameter name="uid" value="ex-cedar"/>
    <Parameter name="name" value="EX CEDAR"/>
  </Configuration>
  <Contents>
    <Content ignore="false" zipEntry="a.kml">
      <Parameter name="uid" value="9a0f-content-uid"/>
      <Parameter name="name" value="a.kml"/>
    </Content>
  </Contents>
</MissionPackageManifest>
"""
    p = tmp_path / "real.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("MANIFEST/manifest.xml", manifest)
        z.writestr("a.kml", KML.format(doc="Ground", pm="AO CEDAR", when=""))

    got = pinecone_packages.read_package(str(p), str(tmp_path / "out"))

    assert got.uid == "ex-cedar"
    assert got.name == "EX CEDAR"


def test_a_damaged_entry_is_refused_rather_than_killing_the_request(tmp_path: Path) -> None:
    """A corrupt deflate stream raises zlib.error, which is neither OSError nor BadZipFile, so it
    escaped every handler: the server logged a traceback and the client got no answer at all."""
    import pinecone_packages

    p = pack(tmp_path, {"a.kml": KML.format(doc="D", pm="X", when="")})
    raw = bytearray(p.read_bytes())
    # Corrupt the compressed body while leaving the central directory intact.
    marker = raw.find(b"<?xml", raw.find(b"a.kml"))
    if marker == -1:  # stored rather than deflated; corrupt the first byte after the local header
        marker = raw.find(b"a.kml") + len("a.kml")
    raw[marker : marker + 20] = b"\xff" * 20
    p.write_bytes(bytes(raw))

    with pytest.raises((pinecone_packages.BadPackage, pinecone_packages.NotAPackage)) as e:
        pinecone_packages.read_package(str(p), str(tmp_path / "out"))
    assert str(e.value), "and it says something rather than nothing"
