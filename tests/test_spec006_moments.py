"""Spec 006, named moments: a time and a name, kept on the box, capped and text-safe.

Everything here is synthetic."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def call(url: str, data: dict | None = None, method: str | None = None) -> tuple[int, str]:
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method or ("POST" if body is not None else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def start(port: int, data: Path, state: Path) -> subprocess.Popen:
    p = subprocess.Popen(
        [sys.executable, str(ROOT / "serve.py"), "--port", str(port), "--data", str(data), "--state", str(state)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    for _ in range(60):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/version", timeout=1).read()
            break
        except Exception:
            time.sleep(0.1)
    return p


@pytest.fixture()
def box(tmp_path: Path):
    data = tmp_path / "data"
    data.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    port = 9700 + (os.getpid() % 50)
    p = start(port, data, state)
    try:
        yield f"http://127.0.0.1:{port}", data, state, port
    finally:
        p.terminate()
        p.wait(timeout=5)


T0 = 1_788_426_000_000  # 3 September 2026, 09:00 UTC


def test_a_moment_is_kept_and_listed(box) -> None:
    base, *_ = box
    code, body = call(f"{base}/api/moments", {"at": T0, "name": "Passage of lines"})
    assert code == 200, body
    made = json.loads(body)
    assert made["at"] == T0 and made["name"] == "Passage of lines" and made["id"]
    assert made["created"], "when it was made is kept"
    assert made["promoted"] is False

    code, body = call(f"{base}/api/moments")
    assert code == 200
    listed = json.loads(body)
    assert [m["name"] for m in listed["moments"]] == ["Passage of lines"]
    assert listed["promoted_cap"] >= 1 and listed["promoted"] == 0, "the budget is visible on every read"


def test_moments_survive_the_server_restarting(box) -> None:
    base, data, state, port = box
    call(f"{base}/api/moments", {"at": T0, "name": "Contact point"})
    # A second server, a separate process, on the same state directory while the first still
    # runs: it reads the file cold, which is the cross-process persistence the criterion asks for.
    code, body = call(f"{base}/api/moments")
    assert len(json.loads(body)["moments"]) == 1
    p = start(port + 1, data, state)
    try:
        code, body = call(f"http://127.0.0.1:{port + 1}/api/moments")
        assert code == 200
        assert [m["name"] for m in json.loads(body)["moments"]] == ["Contact point"]
    finally:
        p.terminate()
        p.wait(timeout=5)


def test_a_moment_can_be_renamed(box) -> None:
    base, *_ = box
    _, body = call(f"{base}/api/moments", {"at": T0, "name": "0412"})
    mid = json.loads(body)["id"]
    code, body = call(f"{base}/api/moments/{mid}", {"name": "0412, contact point wrong"})
    assert code == 200, body
    assert json.loads(body)["name"] == "0412, contact point wrong"
    _, body = call(f"{base}/api/moments")
    assert json.loads(body)["moments"][0]["name"] == "0412, contact point wrong"


def test_a_moment_can_be_deleted_without_touching_the_rest(box) -> None:
    base, *_ = box
    ids = []
    for i, name in enumerate(("one", "two", "three")):
        _, body = call(f"{base}/api/moments", {"at": T0 + i * 60_000, "name": name})
        ids.append(json.loads(body)["id"])
    code, body = call(f"{base}/api/moments/{ids[1]}/delete", {})
    assert code == 200, body
    _, body = call(f"{base}/api/moments")
    assert [m["name"] for m in json.loads(body)["moments"]] == ["one", "three"]
    code, _ = call(f"{base}/api/moments/{ids[1]}/delete", {})
    assert code == 404, "deleting it again is told, not silently agreed with"


def test_the_promoted_cap_is_hard(box) -> None:
    base, *_ = box
    _, body = call(f"{base}/api/moments")
    cap = json.loads(body)["promoted_cap"]
    ids = []
    for i in range(cap + 1):
        _, body = call(f"{base}/api/moments", {"at": T0 + i * 60_000, "name": f"m{i}"})
        ids.append(json.loads(body)["id"])
    for mid in ids[:cap]:
        code, body = call(f"{base}/api/moments/{mid}", {"promoted": "yes"})
        assert code == 200, body
    code, body = call(f"{base}/api/moments/{ids[cap]}", {"promoted": "yes"})
    assert code == 409, "one past the budget is refused"
    assert str(cap) in body, "and the refusal says what the budget is"
    _, body = call(f"{base}/api/moments")
    assert json.loads(body)["promoted"] == cap


def test_demoting_frees_a_slot(box) -> None:
    base, *_ = box
    _, body = call(f"{base}/api/moments")
    cap = json.loads(body)["promoted_cap"]
    ids = []
    for i in range(cap + 1):
        _, body = call(f"{base}/api/moments", {"at": T0 + i * 60_000, "name": f"m{i}"})
        ids.append(json.loads(body)["id"])
    for mid in ids[:cap]:
        call(f"{base}/api/moments/{mid}", {"promoted": "yes"})
    code, _ = call(f"{base}/api/moments/{ids[0]}", {"promoted": "no"})
    assert code == 200
    code, body = call(f"{base}/api/moments/{ids[cap]}", {"promoted": "yes"})
    assert code == 200, body


def test_a_name_is_kept_as_text_not_interpreted(box) -> None:
    base, *_ = box
    nasty = '<img src=x onerror="alert(1)"> & "quotes"'
    code, body = call(f"{base}/api/moments", {"at": T0, "name": nasty})
    assert code == 200, body
    assert json.loads(body)["name"] == nasty, "stored exactly as given; the page's job is to render it as text"


def test_the_limits_are_stated_when_they_bite(box) -> None:
    base, *_ = box
    code, body = call(f"{base}/api/moments", {"at": T0, "name": "x" * 1000})
    assert code == 400
    assert "characters" in body.lower()
    code, body = call(f"{base}/api/moments", {"at": "noon", "name": "fine"})
    assert code == 400
    assert "time" in body.lower()
    code, body = call(f"{base}/api/moments", {"at": T0, "name": "   "})
    assert code == 400, "a blank name is refused; the clock is the provisional name, the page supplies it"


# From the review.


def test_a_damaged_store_degrades_to_what_can_be_read(box) -> None:
    """A hand-edited entry with a string or a null for its time raised in the list's sort and killed
    every read of the list; the threat note said the store degraded to empty, and it did not."""
    base, data, state, port = box
    call(f"{base}/api/moments", {"at": T0, "name": "good"})
    store = state / "moments.json"
    damaged = json.loads(store.read_text())
    damaged += [
        {"id": "h1", "at": "noon", "name": "string time"},
        {"id": "h2", "at": None, "name": "null time"},
        {"id": "h3", "at": True, "name": "a bool is not a time"},
        {"id": "h4", "at": T0, "name": 42},
        {"id": "", "at": T0, "name": "no id"},
        "not even a dict",
    ]
    store.write_text(json.dumps(damaged))

    code, body = call(f"{base}/api/moments")

    assert code == 200, body
    assert [m["name"] for m in json.loads(body)["moments"]] == [
        "good"
    ], "the damaged entries are dropped, the good one stays"


def test_the_count_limit_is_stated_when_it_bites(box) -> None:
    """Criterion 5's other half, which the first version of the limits test never approached."""
    base, *_ = box
    _, body = call(f"{base}/api/moments")
    limit = json.loads(body)["limit"]
    for i in range(limit):
        code, body = call(f"{base}/api/moments", {"at": T0 + i * 1000, "name": f"m{i}"})
        assert code == 200, body
    code, body = call(f"{base}/api/moments", {"at": T0 + limit * 1000, "name": "one too many"})
    assert code == 409
    assert str(limit) in body


def test_a_moments_time_is_bounded(box) -> None:
    base, *_ = box
    for bad in ("0", "-5", "10000000000000000000000", "1_000"):
        code, body = call(f"{base}/api/moments", {"at": bad, "name": "x"})
        assert code == 400, (bad, body)
