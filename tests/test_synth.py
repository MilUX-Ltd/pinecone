"""The synthetic fixture: the only bundle that is ever committed."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_fixture_is_deterministic_and_has_the_shapes_slice_one_needs(root: Path, tmp_path: Path) -> None:
    out = tmp_path / "s.json"
    subprocess.run([sys.executable, str(root / "synth.py"), str(out)], check=True, capture_output=True)
    fresh = json.loads(out.read_text())
    committed = json.loads((root / "data" / "synthetic.json").read_text())
    assert fresh["tracks"] == committed["tracks"], "regenerate data/synthetic.json when synth.py changes"
    tracks = {t["callsign"]: t for t in fresh["tracks"]}
    assert len(tracks) == 6
    assert sum(1 for t in tracks.values() if t["platform"] == "Meshtastic") >= 1
    assert any(t["platform"].startswith("ATAK") for t in tracks.values())
    gaps = [b[0] - a[0] for a, b in zip(tracks["SYN-TRK2"]["points"], tracks["SYN-TRK2"]["points"][1:], strict=False)]
    assert max(gaps) > 20 * 60_000, "the deliberate dropout must be longer than any threshold"
    for t in tracks.values():
        times = [p[0] for p in t["points"]]
        assert times == sorted(times)
        assert all(abs(p[1]) > 1e-6 for p in t["points"])
    assert all(cs.startswith("SYN-") for cs in tracks), "nothing in the fixture can be mistaken for a person"
