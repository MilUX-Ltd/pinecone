"""Spec 014: a clean removal. The box is built by install.sh itself and then removed, so these
assert against the layout the installer really produces rather than a hand-made copy of it.
PINECONE_ROOT relocates every path and the fake commands in tests/fakebin stand in for the box;
the script is never invoked without PINECONE_ROOT set. The live proof is a real box."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

FAKEBIN = Path(__file__).resolve().parent / "fakebin"
HERE = Path(__file__).resolve().parent.parent

UNIT = "etc/systemd/system/pinecone.service"
RECUNIT = "etc/systemd/system/pinecone-recorder.service"
ARCHIVE = "var/lib/pinecone/archive/pinecone.db"


def _env(root: Path, log: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["PATH"] = f"{FAKEBIN}:{env['PATH']}"
    env["PINECONE_ROOT"] = str(root)
    env["PINECONE_FAKE_LOG"] = str(log)
    return env


def installed_box(tmp_path: Path) -> Path:
    """A box with TAK Server on it, then Pinecone installed onto it by install.sh."""
    root = tmp_path / "box"
    (root / "opt/tak/conf/retention").mkdir(parents=True)
    conf = '<Configuration><repository><connection url="jdbc:postgresql://127.0.0.1:5432/cot" username="martiuser" password="SECRETPASS"/></repository></Configuration>'
    (root / "opt/tak/CoreConfig.xml").write_text(conf)
    (root / "opt/tak/CoreConfig.xml").chmod(0o600)
    ex = root / "opt/tak/CoreConfig.example.xml"
    ex.write_text(conf)
    ex.chmod(0o674)
    (root / "opt/tak/conf/retention/retention-policy.yml").write_text("dataRetentionMap:\n  cot: null\n  files: null\n")
    (root / "etc/systemd/system").mkdir(parents=True)

    r = subprocess.run(
        ["bash", str(HERE / "install.sh"), "--yes"],
        cwd=HERE,
        env=_env(root, root / "install.log"),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert r.returncode == 0, "the fixture could not install: " + r.stdout + r.stderr
    # the removal only means anything if these were there to remove
    assert (root / "opt/pinecone").is_dir() and (root / "etc/pinecone/pinecone.env").exists()
    assert (root / UNIT).exists() and (root / RECUNIT).exists()
    (root / ARCHIVE).parent.mkdir(parents=True, exist_ok=True)
    (root / ARCHIVE).write_text("where people were")
    return root


def uninstall(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(HERE / "uninstall.sh"), *args],
        cwd=HERE,
        env=_env(root, root / "uninstall.log"),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def uninstall_log(root: Path) -> str:
    p = root / "uninstall.log"
    return p.read_text() if p.exists() else ""


def tree_hash(d: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(p for p in d.rglob("*") if p.is_file()):
        h.update(str(f.relative_to(d)).encode())
        h.update(f.read_bytes())
    return h.hexdigest()


def assert_pinecone_is_gone(root: Path) -> None:
    assert not (root / "opt/pinecone").exists(), "/opt/pinecone survived"
    assert not (root / "etc/pinecone").exists(), "/etc/pinecone survived"
    assert not (root / UNIT).exists(), "the unit file survived"
    assert not (root / RECUNIT).exists(), "the recorder unit file survived"
    log = uninstall_log(root)
    assert "systemctl disable" in log, "the units were never disabled"
    assert "pinecone.service" in log and "pinecone-recorder.service" in log, "both units are disabled"
    assert "userdel" in log and "pinecone" in log, "the user was never deleted"
    assert "DROP ROLE" in log.upper(), "the role was never dropped"


def test_a_default_removal_takes_the_box_back_and_keeps_the_archive(tmp_path: Path) -> None:
    root = installed_box(tmp_path)
    r = uninstall(root)
    assert r.returncode == 0, r.stdout + r.stderr
    assert_pinecone_is_gone(root)
    assert (root / "var/lib/pinecone").is_dir(), "the archive tree was removed without --purge"
    assert (root / ARCHIVE).read_text() == "where people were", "the archive was altered"
    assert "kept" in r.stdout.lower(), "the closing line does not say the archive was kept"


def test_purge_removes_the_archive_too(tmp_path: Path) -> None:
    root = installed_box(tmp_path)
    r = uninstall(root, "--purge")
    assert r.returncode == 0, r.stdout + r.stderr
    assert_pinecone_is_gone(root)
    assert not (root / "var/lib/pinecone").exists(), "--purge left the archive behind"
    assert "kept" not in r.stdout.lower(), "the closing line claims the archive was kept after a purge"


def test_an_unknown_argument_removes_nothing(tmp_path: Path) -> None:
    root = installed_box(tmp_path)
    before = tree_hash(root / "opt/pinecone")
    r = uninstall(root, "--dry-run")
    assert r.returncode == 2, "an argument it does not understand must stop: " + r.stdout + r.stderr
    assert "unknown option" in (r.stdout + r.stderr).lower()
    assert (root / "opt/pinecone").is_dir() and tree_hash(root / "opt/pinecone") == before
    assert (root / "etc/pinecone/pinecone.env").exists()
    assert (root / UNIT).exists() and (root / RECUNIT).exists()
    assert (root / ARCHIVE).exists()
    assert "DROP ROLE" not in uninstall_log(root).upper(), "it reached the database before refusing"


def test_an_unknown_argument_before_purge_removes_nothing(tmp_path: Path) -> None:
    root = installed_box(tmp_path)
    r = uninstall(root, "--oops", "--purge")
    assert r.returncode == 2, "the whole command line is read, not just the first argument"
    assert (root / "opt/pinecone").is_dir()
    assert (root / ARCHIVE).exists(), "it purged on a command line it had already rejected"


def test_only_the_pinecone_role_is_dropped(tmp_path: Path) -> None:
    root = installed_box(tmp_path)
    assert uninstall(root).returncode == 0
    sql = "".join(line for line in uninstall_log(root).splitlines() if line.startswith("psql:")).upper()
    assert "DROP ROLE" in sql and "PINECONE" in sql
    assert "DROP DATABASE" not in sql, "it drops a database"
    assert "MARTIUSER" not in sql, "it touches TAK Server's own role"
    assert "DROP ROLE IF EXISTS PINECONE" in sql.replace('"', ""), "the role is dropped by name and tolerantly"
    assert "SECRETPASS" not in uninstall_log(root), "a credential reached the log"


def test_tak_server_is_untouched(tmp_path: Path) -> None:
    root = installed_box(tmp_path)
    before = tree_hash(root / "opt/tak")
    assert uninstall(root, "--purge").returncode == 0
    assert (root / "opt/tak").is_dir(), "TAK Server's tree was removed"
    assert tree_hash(root / "opt/tak") == before, "something under /opt/tak changed"


def test_running_it_twice_is_harmless(tmp_path: Path) -> None:
    root = installed_box(tmp_path)
    assert uninstall(root).returncode == 0
    r = uninstall(root)
    assert r.returncode == 0, "the second run failed on what the first had already removed: " + r.stdout + r.stderr
    assert (root / "var/lib/pinecone").is_dir(), "the second run took the archive"
    assert "nothing to remove" in r.stdout.lower(), "it reported a removal it did not make"
