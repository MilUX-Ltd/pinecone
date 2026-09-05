"""Spec 001, criterion 4: the page shows what was found and where, the player stays at /replay,
and the JSON carries no password."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

FAKEBIN = Path(__file__).resolve().parent / "fakebin"
ROOT = Path(__file__).resolve().parent.parent

DISCOVERY = {
    "pinecone": {"version": "0.2.0"},
    "tak": {
        "version": "5.8-RELEASE75",
        "version_source": "dpkg",
        "unit": "takserver.service",
        "unit_state": "active",
        "ports": {"8089": True, "8443": True, "8446": True, "8444": False, "9001": False, "5432": True},
    },
    "database": {
        "host": "127.0.0.1",
        "port": 5432,
        "database": "cot",
        "username": "martiuser",
        "source": "/opt/tak/CoreConfig.xml",
        "timezone": None,
    },
    "files": {
        "/opt/tak/CoreConfig.xml": {"mode": "600", "owner": "tak:tak", "finding": None},
        "/opt/tak/CoreConfig.example.xml": {
            "mode": "674",
            "owner": "tak:tak",
            "finding": "world-readable and carries the database password",
        },
    },
    "retention": {
        "ttls": {"cot": None, "files": None},
        "purges": False,
        "source": "/opt/tak/conf/retention/retention-policy.yml",
    },
    "rows": None,
    "credential": {
        "role": "pinecone",
        "grant": "SELECT on cot_router",
        "created": True,
        "statement": "Pinecone reads with its own role pinecone, SELECT on cot_router only. The martiuser password in CoreConfig.xml was read for nothing and used for nothing.",
    },
    "discovered_at": "2026-09-04T15:00:00Z",
}


@pytest.fixture()
def page(tmp_path: Path):
    disc = tmp_path / "discovery.json"
    disc.write_text(json.dumps(DISCOVERY))
    env = dict(os.environ)
    env["PATH"] = f"{FAKEBIN}:{env['PATH']}"
    env["PINECONE_DISCOVERY"] = str(disc)
    env["PGUSER"] = "pinecone"
    env["PGPASSWORD"] = "not-a-real-password-xyz"
    env["PGDATABASE"] = "cot"
    port = 8790 + (os.getpid() % 100)
    p = subprocess.Popen(
        [sys.executable, str(ROOT / "serve.py"), "--port", str(port), "--data", str(tmp_path)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/version", timeout=1).read()
                break
            except Exception:
                time.sleep(0.1)
        yield f"http://127.0.0.1:{port}"
    finally:
        p.terminate()
        p.wait(timeout=5)


def get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_the_page_shows_what_was_found_and_where(page: str) -> None:
    # Decided 4 Sep 2026: the replay is the front door; this report moved to /status.
    code, html = get(f"{page}/status")
    assert code == 200
    for needle in (
        "Pinecone 0.2.0",
        "5.8-RELEASE75",
        "dpkg",
        "/opt/tak/CoreConfig.xml",
        "148,298",
        "2026-08-08",
        "2026-09-04",
        "nothing is purged",
        "retention-policy.yml",
        "America/New_York",
        "SELECT on cot_router",
        "used for nothing",
        "world-readable",
        "127.0.0.1",
        "no authentication",
        "/replay",
    ):
        assert needle in html, needle
    assert "not-a-real-password-xyz" not in html


def test_the_player_is_still_there(page: str) -> None:
    code, html = get(f"{page}/replay")
    assert code == 200 and 'id="map"' in html and "app.js" in html
    # and it is what you land on
    code, html = get(f"{page}/")
    assert code == 200 and 'id="map"' in html and 'id="gear"' in html


def test_the_json_has_no_password(page: str) -> None:
    code, body = get(f"{page}/api/discovery")
    assert code == 200
    d = json.loads(body)
    assert d["tak"]["version"] == "5.8-RELEASE75" and d["rows"]["count"] == 148298
    assert "not-a-real-password-xyz" not in body

    def keys(o):
        if isinstance(o, dict):
            for k, v in o.items():
                yield k
                yield from keys(v)
        elif isinstance(o, list):
            for v in o:
                yield from keys(v)

    assert not [k for k in keys(d) if "pass" in k.lower()]


def test_without_a_discovery_file_the_page_says_so(tmp_path: Path) -> None:
    env = dict(os.environ)
    env["PINECONE_DISCOVERY"] = str(tmp_path / "missing.json")
    port = 8890 + (os.getpid() % 100)
    p = subprocess.Popen(
        [sys.executable, str(ROOT / "serve.py"), "--port", str(port), "--data", str(tmp_path)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/version", timeout=1).read()
                break
            except Exception:
                time.sleep(0.1)
        code, html = get(f"http://127.0.0.1:{port}/status")
        assert code == 200 and "install.sh" in html and "/replay" in html
    finally:
        p.terminate()
        p.wait(timeout=5)
