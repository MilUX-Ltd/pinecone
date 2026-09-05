"""Build a synthetic TAK data package carrying overlays, for demonstrating and testing the replay.

Every fixture is synthetic. No real exercise material goes anywhere near this.
Drawn over Andover, because that is the basemap the synthetic fixture uses.
"""

from __future__ import annotations

import argparse
import zipfile
from pathlib import Path

# Andover, roughly. A boundary, two phase lines and an objective.
CENTRE = (51.2113, -1.4870)

MANIFEST = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<MissionPackageManifest version="2">
  <Configuration>
    <Parameter name="uid" value="{uid}"/>
    <Parameter name="name" value="{name}"/>
    <Parameter name="onReceiveImport" value="true"/>
    <Parameter name="onReceiveDelete" value="false"/>
  </Configuration>
  <Contents>
{entries}
  </Contents>
</MissionPackageManifest>
"""

ENTRY = '    <Content ignore="false" zipEntry="{path}"/>'


def kml_boundary() -> str:
    lat, lon = CENTRE
    d = 0.012
    ring = " ".join(f"{lon + a:.6f},{lat + b:.6f},0" for a, b in ((-d, -d), (d, -d), (d, d), (-d, d), (-d, -d)))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>Exercise boundary</name>
    <Placemark>
      <name>AO CEDAR</name>
      <Style><LineStyle><color>ff2f7fff</color><width>3</width></LineStyle>
             <PolyStyle><color>222f7fff</color></PolyStyle></Style>
      <Polygon><outerBoundaryIs><LinearRing><coordinates>{ring}</coordinates>
      </LinearRing></outerBoundaryIs></Polygon>
    </Placemark>
  </Document>
</kml>
"""


def kml_phase_lines() -> str:
    lat, lon = CENTRE

    def line(dy: float, name: str, when: str) -> str:
        c = f"{lon - 0.012:.6f},{lat + dy:.6f},0 {lon + 0.012:.6f},{lat + dy:.6f},0"
        return f"""    <Placemark>
      <name>{name}</name>
      <TimeSpan><begin>{when}</begin></TimeSpan>
      <Style><LineStyle><color>ff00d7ff</color><width>2</width></LineStyle></Style>
      <LineString><coordinates>{c}</coordinates></LineString>
    </Placemark>"""

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>Phase lines</name>
{line(0.004, "PL GRANITE", "2026-09-03T07:00:00Z")}
{line(-0.004, "PL SLATE", "2026-09-03T09:30:00Z")}
  </Document>
</kml>
"""


def cot_objective() -> str:
    lat, lon = CENTRE
    return f"""<?xml version="1.0" standalone="yes"?>
<event version="2.0" uid="OBJ-CEDAR-1" type="b-m-p-w" how="h-g-i-g-o"
       time="2026-09-03T06:00:00Z" start="2026-09-03T06:00:00Z" stale="2026-09-04T06:00:00Z">
  <point lat="{lat + 0.006:.6f}" lon="{lon + 0.005:.6f}" hae="90" ce="9999999" le="9999999"/>
  <detail><contact callsign="OBJ CEDAR"/><remarks>Objective</remarks></detail>
</event>
"""


def cot_reported_contact() -> str:
    lat, lon = CENTRE
    # Hostile, and REPORTED, not observed: how="h-e" (human estimated).
    return f"""<?xml version="1.0" standalone="yes"?>
<event version="2.0" uid="RPT-HOSTILE-1" type="a-h-G" how="h-e"
       time="2026-09-03T08:10:00Z" start="2026-09-03T08:10:00Z" stale="2026-09-03T10:10:00Z">
  <point lat="{lat - 0.007:.6f}" lon="{lon - 0.006:.6f}" hae="88" ce="250" le="9999999"/>
  <detail><contact callsign="REPORTED CONTACT"/><remarks>Called in by MilUX, not observed</remarks></detail>
</event>
"""


FILES = {
    "boundary.kml": kml_boundary,
    "phase-lines.kml": kml_phase_lines,
    "objective.cot": cot_objective,
    "reported-contact.cot": cot_reported_contact,
}


def build(out: Path, name: str = "EX CEDAR (synthetic)") -> Path:
    entries = "\n".join(ENTRY.format(path=p) for p in FILES)
    manifest = MANIFEST.format(uid="synthetic-ex-cedar", name=name, entries=entries)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("MANIFEST/manifest.xml", manifest)
        for path, fn in FILES.items():
            z.writestr(path, fn())
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="data/synthetic-mission-pack.zip")
    a = ap.parse_args()
    p = Path(a.out)
    p.parent.mkdir(parents=True, exist_ok=True)
    build(p)
    print(f"wrote {p}: {p.stat().st_size} bytes, {len(FILES)} overlays plus a manifest")


if __name__ == "__main__":
    main()
