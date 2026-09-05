#!/usr/bin/env python3
"""Reading a TAK data package: the ground an exercise was run on.

The first thing Pinecone opens that somebody else made. Everything before it read the TAK Server's
own database, its own configuration, and map files already on the box; a mission pack arrives from
outside, is a zip full of XML, and an operator will be told to point Pinecone at it. Threat note
005 is the reasoning; this module is where it is enforced.

Nothing here trusts the archive. Entry names are resolved and refused unless they stay inside the
extraction directory, XML is parsed with entity resolution off, and the total expanded size is
capped. Standard library only (ADR-002).
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from typing import Any

# Two different limits for two different risks. Nothing here is ever written to disk, so the first
# bounds how much of a package is read into memory across all its entries, on a box that is also
# running TAK Server. The second protects the parse: reading a 200 MB KML into a DOM is its own
# denial of service, and it sits well under the first.
MAX_EXPANDED_BYTES = 256 * 1024 * 1024
MAX_XML_BYTES = 32 * 1024 * 1024
MAX_ENTRIES = 2000
MANIFEST_NAMES = ("MANIFEST/manifest.xml", "manifest.xml")

KML_NS = "{http://www.opengis.net/kml/2.2}"


class NotAPackage(ValueError):
    """The zip carries no manifest, so it is not a data package."""


class BadPackage(ValueError):
    """A data package that cannot be trusted or cannot be read. Carries the rule that stopped it."""


@dataclass
class Shape:
    """One thing drawn on the map: a boundary, a phase line, an objective, a reported contact."""

    kind: str  # polygon | line | point
    label: str
    coordinates: list[list[float]]  # [[lat, lon], ...]
    begin_ms: int | None = None
    end_ms: int | None = None
    undated: bool = True
    window_unreadable: bool = False
    reported: bool = False
    ce: float | None = None
    remarks: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "label": self.label,
            "coordinates": self.coordinates,
            "begin_ms": self.begin_ms,
            "end_ms": self.end_ms,
            "undated": self.undated,
            "window_unreadable": self.window_unreadable,
            "reported": self.reported,
            "ce": self.ce,
            "remarks": self.remarks,
        }


@dataclass
class Overlay:
    path: str
    kind: str  # kml | cot
    name: str
    shapes: list[Shape] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "kind": self.kind, "name": self.name, "shapes": [s.as_dict() for s in self.shapes]}


@dataclass
class Package:
    uid: str
    name: str
    overlays: list[Overlay] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "name": self.name,
            "ignored": self.ignored,
            "overlays": [o.as_dict() for o in self.overlays],
        }


def _read_entry(zf: zipfile.ZipFile, entry: str) -> bytes:
    """The bytes of one entry, or a refusal that says which entry and why."""
    try:
        zf.getinfo(entry)
    except KeyError as e:
        raise BadPackage(f"{entry} is not in the package") from e
    try:
        return zf.read(entry)
    except Exception as e:
        # A damaged deflate stream raises zlib.error, and a bad CRC raises BadZipFile. Neither is
        # an OSError, so both used to escape every handler and kill the request with no answer.
        raise BadPackage(f"{entry} could not be read out of the package: {e.__class__.__name__}") from e


def _read_xml(zf: zipfile.ZipFile, entry: str) -> str:
    """The text of one entry, refused if it is too large to parse or cannot be read at all."""
    try:
        size = zf.getinfo(entry).file_size
    except KeyError as e:
        raise BadPackage(f"{entry} is not in the package") from e
    if size > MAX_XML_BYTES:
        raise BadPackage(
            f"{entry} is {size // 1024**2} MB, past the {MAX_XML_BYTES // 1024**2} MB limit for a "
            "file this has to parse"
        )
    return _read_entry(zf, entry).decode("utf-8", "replace")


def safe_parse(text: str) -> ET.Element:
    """Parse XML from a package with entity resolution off.

    A KML that declares an external entity pointing at /etc/pinecone/pinecone.env would put the
    database credential into an overlay label, and overlays are drawn on a page with no
    authentication. A doctype is refused outright rather than resolved and stripped, because
    stripping is a game you lose eventually.
    """
    if re.search(r"<!DOCTYPE", text, re.I):
        raise BadPackage("the XML declares a doctype, which a mission pack has no need for")
    if re.search(r"<!ENTITY", text, re.I):
        raise BadPackage("the XML declares an entity, which a mission pack has no need for")
    # S314 says to use defusedxml for untrusted XML, and it is right to say so. ADR-002 keeps this
    # product on the standard library, so the defence is above instead of in a dependency: a
    # doctype or an entity declaration is refused outright, before a parser sees the text. Every
    # XML external entity attack, and both the billion-laughs and quadratic-blowup expansions,
    # need one or the other. ElementTree also does not resolve external entities on its own. The
    # size of what is parsed is capped separately. Reasoning in docs/security/threat-note-005.md;
    # if this file ever needs to accept a doctype, this suppression stops being honest.
    parser = ET.XMLParser()  # noqa: S314
    try:
        parser.feed(text)
        return parser.close()
    except ET.ParseError as e:
        raise BadPackage(f"the XML could not be read: {e}") from e


def inside(directory: str, name: str) -> str:
    """Where an entry may be written, or a refusal. The classic zip traversal is the whole attack."""
    target = os.path.realpath(os.path.join(directory, name))
    root = os.path.realpath(directory)
    if target != root and not target.startswith(root + os.sep):
        raise BadPackage(f"the entry {name!r} would be written outside the package's own directory")
    return target


def _coords(text: str) -> list[list[float]]:
    """KML coordinates are lon,lat[,alt] triples; everything else here is lat,lon."""
    out: list[list[float]] = []
    for chunk in (text or "").split():
        parts = chunk.split(",")
        if len(parts) < 2:
            continue
        try:
            lon, lat = float(parts[0]), float(parts[1])
        except ValueError:
            continue
        out.append([round(lat, 7), round(lon, 7)])
    return out


class UnreadableTime(ValueError):
    """A time that was there and could not be read. Not the same as no time at all."""


# KML allows a dateTime with or without a zone, and a plain date, a year and month, or a year.
_TIME_FORMATS = (
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y-%m-%d %H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
    "%Y-%m",
    "%Y",
)


def _ms(text: str | None) -> int | None:
    """A time in milliseconds, or None if there was nothing to read.

    Raises UnreadableTime if there was something and it could not be read, so the caller can tell
    absent from unreadable. Silently turning one into the other made an overlay whose window could
    not be parsed draw throughout the replay and say it carried no window, which is the opposite of
    what the pack said.
    """
    if text is None or not text.strip():
        return None
    from datetime import datetime, timezone

    t = text.strip().replace("Z", "+00:00")
    for fmt in _TIME_FORMATS:
        try:
            when = datetime.strptime(t, fmt)
        except ValueError:
            continue
        # A KML time with no zone is read as UTC, which is what the rest of this product does.
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return int(when.timestamp() * 1000)
    raise UnreadableTime(text.strip())


def _kml_shapes(root: ET.Element) -> tuple[str, list[Shape]]:
    doc_name = ""
    el = root.find(f".//{KML_NS}Document/{KML_NS}name")
    if el is not None and el.text:
        doc_name = el.text.strip()
    shapes: list[Shape] = []
    for pm in root.iter(f"{KML_NS}Placemark"):
        name_el = pm.find(f"{KML_NS}name")
        label = (name_el.text or "").strip() if name_el is not None else ""
        begin = pm.find(f".//{KML_NS}TimeSpan/{KML_NS}begin")
        end = pm.find(f".//{KML_NS}TimeSpan/{KML_NS}end")
        declared = (begin is not None and (begin.text or "").strip()) or (end is not None and (end.text or "").strip())
        unreadable = False
        try:
            begin_ms = _ms(begin.text if begin is not None else None)
            end_ms = _ms(end.text if end is not None else None)
        except UnreadableTime:
            # The pack declared a window this cannot read. Draw the overlay rather than hide it,
            # and say why, instead of pretending it never had one.
            begin_ms = end_ms = None
            unreadable = True
        for kind, path in (
            ("polygon", f".//{KML_NS}Polygon//{KML_NS}coordinates"),
            ("line", f".//{KML_NS}LineString/{KML_NS}coordinates"),
            ("point", f".//{KML_NS}Point/{KML_NS}coordinates"),
        ):
            c = pm.find(path)
            if c is None:
                continue
            coords = _coords(c.text or "")
            if not coords:
                continue
            shapes.append(
                Shape(
                    kind=kind,
                    label=label,
                    coordinates=coords,
                    begin_ms=begin_ms,
                    end_ms=end_ms,
                    # An overlay with no window applies throughout, and says so rather than
                    # implying a window it does not have. One whose window could not be read is a
                    # third thing, and says that instead.
                    undated=not declared,
                    window_unreadable=unreadable,
                )
            )
            break
    return doc_name, shapes


# What counts as reported rather than observed, and why the rule is shaped this way.
#
# Two things make an item somebody's report. Either a human put it there, which CoT says with a
# `how` beginning `h-` (`h-e` estimated, `h-g-i-g-o` typed in, `h-t` transcribed), or it is a
# contact nobody friendly is transmitting: hostile, suspect, unknown and neutral tracks in a
# mission pack are all somebody's assertion about somebody else, however they were entered.
#
# The `a-` test is what keeps this honest in the other direction. A control measure drawn by a
# planner, a boundary or an objective, also carries `h-g-i-g-o`, and calling it "reported" would be
# noise: it is a plan, not a claim about where anything was. Only contacts, CoT types beginning
# `a-`, are judged here.
#
# The first version of this list carried `h-g-i-g-o-r`, which is not a CoT value and was a typo for
# the commonest human-entry value of all, so a spot report of an unknown contact was drawn as
# though somebody had watched it happen. Spec 005 calls this the line the product's thesis rests
# on, and that is exactly the direction it must not fail in.
HUMAN_ENTERED = ("h-e", "h-g-i-g-o", "h-t")
REPORTED_AFFILIATIONS = ("a-h", "a-s", "a-u", "a-n")  # hostile, suspect, unknown, neutral


def _cot_shape(root: ET.Element, fallback_label: str) -> Shape | None:
    point = root.find("point")
    if point is None:
        return None
    try:
        lat, lon = float(point.get("lat", "")), float(point.get("lon", ""))
    except ValueError:
        return None
    contact = root.find(".//contact")
    label = (contact.get("callsign") if contact is not None else "") or fallback_label
    remarks_el = root.find(".//remarks")
    remarks = (remarks_el.text or "").strip() if remarks_el is not None else ""
    how = (root.get("how") or "").strip()
    cot_type = (root.get("type") or "").strip()
    is_contact = cot_type.startswith("a-")
    reported = is_contact and (
        any(how.startswith(h) for h in HUMAN_ENTERED) or any(cot_type.startswith(a) for a in REPORTED_AFFILIATIONS)
    )
    try:
        ce = float(point.get("ce", ""))
    except ValueError:
        ce = None
    unreadable = False
    try:
        begin_ms = _ms(root.get("start"))
        end_ms = _ms(root.get("stale"))
    except UnreadableTime:
        begin_ms = end_ms = None
        unreadable = True
    declared = bool((root.get("start") or "").strip() or (root.get("stale") or "").strip())
    return Shape(
        kind="point",
        label=f"{label} (reported)" if reported else label,
        coordinates=[[round(lat, 7), round(lon, 7)]],
        begin_ms=begin_ms,
        end_ms=end_ms,
        undated=not declared,
        window_unreadable=unreadable,
        reported=reported,
        ce=ce,
        remarks=remarks,
    )


def read_package(path: str, out_dir: str) -> Package:
    """Read a TAK data package for the ground it carries.

    Refuses anything it cannot trust, and says which rule stopped it. Nothing is written outside
    `out_dir`, and nothing outside it is read.
    """
    try:
        zf = zipfile.ZipFile(path)
    except FileNotFoundError as e:
        raise BadPackage("there is no file at that path") from e
    except PermissionError as e:
        raise BadPackage("that file cannot be read by the service") from e
    except (OSError, zipfile.BadZipFile) as e:
        # The reason is named; the operating system's own message, which carries the path and an
        # errno, is not repeated to a caller on an unauthenticated route.
        raise BadPackage("that file is not a zip archive") from e

    with zf:
        names = zf.namelist()
        if len(names) > MAX_ENTRIES:
            raise BadPackage(f"the package holds {len(names)} entries, more than the limit of {MAX_ENTRIES}")
        total = sum(i.file_size for i in zf.infolist())
        if total > MAX_EXPANDED_BYTES:
            raise BadPackage(
                f"the package expands to {total // 1024**2} MB, past the limit of "
                f"{MAX_EXPANDED_BYTES // 1024**2} MB it is allowed on a box shared with TAK Server"
            )

        manifest_name = next((n for n in MANIFEST_NAMES if n in names), "")
        if not manifest_name:
            raise NotAPackage("no MANIFEST/manifest.xml: that zip is not a TAK data package")

        root = safe_parse(_read_xml(zf, manifest_name))
        # Only the manifest's own Configuration, not every Parameter in the tree: a real ATAK
        # manifest carries per-content uid and name parameters inside each <Content>, and an
        # untargeted walk lets the last of those win. Every fixture in this repository used the
        # self-closing <Content/> form, so nothing here caught it.
        config = root.find("Configuration")
        params = {p.get("name"): p.get("value") for p in config.findall("Parameter")} if config is not None else {}
        pack = Package(uid=params.get("uid") or "package", name=params.get("name") or "unnamed package")

        wanted = [c.get("zipEntry") or "" for c in root.iter("Content") if (c.get("ignore") or "").lower() != "true"]
        # Nothing is written to out_dir; it is the directory the traversal check is measured
        # against, and realpath needs no directory to exist. Making it left a stray directory on
        # every box for every import.
        for entry in wanted:
            if not entry:
                continue
            inside(out_dir, entry)  # refuses a traversal before anything is read
            if entry not in names:
                raise BadPackage(f"the manifest names {entry!r}, which the package does not contain")

        for entry in wanted:
            lower = entry.lower()
            if lower.endswith(".kmz"):
                pack.overlays.extend(_from_kmz(zf, entry, out_dir, pack.ignored))
                continue
            if lower.endswith(".kml"):
                doc, shapes = _kml_shapes(safe_parse(_read_xml(zf, entry)))
                pack.overlays.append(Overlay(path=entry, kind="kml", name=doc or entry, shapes=shapes))
                continue
            if lower.endswith((".cot", ".xml")):
                shape = _cot_shape(safe_parse(_read_xml(zf, entry)), entry)
                pack.overlays.append(Overlay(path=entry, kind="cot", name=entry, shapes=[shape] if shape else []))
                continue
            # Listed, not guessed at: a package carries plenty that is not ground.
            pack.ignored.append(entry)
    return pack


def _from_kmz(zf: zipfile.ZipFile, entry: str, out_dir: str, ignored: list[str]) -> list[Overlay]:
    """A KMZ is a zip inside the zip. Same rules apply one level down, including the promise that
    anything not read is listed rather than dropped in silence. A KMZ inside a KMZ is not followed."""
    import io

    try:
        inner = zipfile.ZipFile(io.BytesIO(_read_entry(zf, entry)))
    except (OSError, zipfile.BadZipFile) as e:
        raise BadPackage(f"{entry} is not a readable KMZ: {e}") from e
    out: list[Overlay] = []
    with inner:
        if sum(i.file_size for i in inner.infolist()) > MAX_EXPANDED_BYTES:
            raise BadPackage(f"{entry} expands past the size a package is allowed")
        for name in inner.namelist():
            if not name.lower().endswith(".kml"):
                if not name.endswith("/"):
                    ignored.append(f"{entry}!{name}")
                continue
            inside(out_dir, name)
            doc, shapes = _kml_shapes(safe_parse(_read_xml(inner, name)))
            out.append(Overlay(path=f"{entry}!{name}", kind="kml", name=doc or name, shapes=shapes))
    return out
