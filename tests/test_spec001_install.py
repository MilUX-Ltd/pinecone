"""Spec 001, criteria 1, 3 and 5: the installer's plan, its confirmation gate, its credential, and
its second run. Run against a box in a directory: PINECONE_ROOT relocates every path and the fake
commands in tests/fakebin stand in for the box. The live proof is a real box."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

FAKEBIN = Path(__file__).resolve().parent / "fakebin"


def box(tmp_path: Path, example_mode: int = 0o674) -> Path:
    root = tmp_path / "box"
    (root / "opt/tak/conf/retention").mkdir(parents=True)
    (root / "opt/tak/CoreConfig.xml").write_text(
        '<Configuration><repository><connection url="jdbc:postgresql://127.0.0.1:5432/cot" username="martiuser" password="SECRETPASS"/></repository></Configuration>'
    )
    (root / "opt/tak/CoreConfig.xml").chmod(0o600)
    ex = root / "opt/tak/CoreConfig.example.xml"
    ex.write_text(
        '<Configuration><repository><connection url="jdbc:postgresql://127.0.0.1:5432/cot" username="martiuser" password="SECRETPASS"/></repository></Configuration>'
    )
    ex.chmod(example_mode)
    (root / "opt/tak/conf/retention/retention-policy.yml").write_text("dataRetentionMap:\n  cot: null\n  files: null\n")
    (root / "etc/systemd/system").mkdir(parents=True)
    return root


def run(
    root: Path, *args: str, stdin: str = "", log: Path | None = None, role_exists: bool = False
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PATH"] = f"{FAKEBIN}:{env['PATH']}"
    env["PINECONE_ROOT"] = str(root)
    env["PINECONE_FAKE_LOG"] = str(log or (root / "fake.log"))
    if role_exists:
        env["PINECONE_FAKE_ROLE_EXISTS"] = "1"
    here = Path(__file__).resolve().parent.parent
    return subprocess.run(
        ["bash", str(here / "install.sh"), *args],
        cwd=here,
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_install_plan_lays_down_the_whole_thing(tmp_path: Path) -> None:
    root = box(tmp_path)
    r = run(root, "--yes")
    assert r.returncode == 0, r.stdout + r.stderr
    out = r.stdout
    assert "useradd" in (root / "fake.log").read_text() and "pinecone" in (root / "fake.log").read_text()
    assert (root / "opt/pinecone/serve.py").exists() and (root / "opt/pinecone/update.sh").exists()
    env_file = root / "etc/pinecone/pinecone.env"
    assert env_file.exists() and oct(env_file.stat().st_mode & 0o777) == "0o640"
    assert "PGUSER=pinecone" in env_file.read_text() and "PGPASSWORD=" in env_file.read_text()
    assert (root / "var/lib/pinecone/data").is_dir() and (root / "var/lib/pinecone/maps").is_dir()
    assert not any((root / "var/lib/pinecone/data").iterdir()), "nothing recorded, nothing pulled"
    unit = (root / "etc/systemd/system/pinecone.service").read_text()
    for line in (
        "User=pinecone",
        "--bind 127.0.0.1",
        "ProtectSystem=strict",
        "NoNewPrivileges=true",
        "EnvironmentFile=/etc/pinecone/pinecone.env",
        "/var/lib/pinecone/data",
    ):
        assert line in unit, line
    assert "recorder" not in unit
    log = (root / "fake.log").read_text()
    assert "systemctl enable" in log and "systemctl start pinecone" in log or "systemctl restart pinecone" in log
    assert (root / "etc/pinecone/discovery.json").exists()
    last = [line for line in out.strip().splitlines() if line.strip()][-2:]
    assert any("http://127.0.0.1:8765/" in line for line in last)
    assert any("loopback" in line.lower() and "no authentication" in line.lower() for line in last)
    assert "SECRETPASS" not in out and "SECRETPASS" not in (root / "etc/pinecone/discovery.json").read_text()


def test_confirmation_gates_the_install(tmp_path: Path) -> None:
    root = box(tmp_path)
    r = run(root, stdin="n\n")
    assert r.returncode == 1 and "not confirmed" in r.stdout + r.stderr
    assert not (root / "etc/pinecone/pinecone.env").exists() and not (root / "opt/pinecone").exists()
    assert "5.8-RELEASE75" in r.stdout and "CoreConfig.xml" in r.stdout, "the report is shown before the question"
    r2 = run(root, stdin="y\n")
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert (root / "etc/pinecone/pinecone.env").exists()


def test_credential_is_narrow_and_generated(tmp_path: Path) -> None:
    root = box(tmp_path)
    r = run(root, "--yes")
    assert r.returncode == 0, r.stdout + r.stderr
    sql = (root / "fake.log").read_text()
    assert "CREATE ROLE pinecone" in sql or 'CREATE ROLE "pinecone"' in sql
    assert "GRANT SELECT ON cot_router TO pinecone" in sql or 'GRANT SELECT ON cot_router TO "pinecone"' in sql
    assert "GRANT ALL" not in sql and "SUPERUSER" not in sql.upper().replace("NOSUPERUSER", "")
    pw = next(
        line for line in (root / "etc/pinecone/pinecone.env").read_text().splitlines() if line.startswith("PGPASSWORD=")
    ).split("=", 1)[1]
    assert len(pw) >= 24 and pw != "SECRETPASS"
    assert pw not in r.stdout, "the generated password is never printed"
    assert "martiuser" in r.stdout and "used for nothing" in r.stdout


def test_second_run_keeps_the_credential_and_changes_nothing_else(tmp_path: Path) -> None:
    root = box(tmp_path)
    assert run(root, "--yes").returncode == 0
    env1 = (root / "etc/pinecone/pinecone.env").read_text()
    unit1 = (root / "etc/systemd/system/pinecone.service").read_text()
    r = run(root, "--yes", role_exists=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (root / "etc/pinecone/pinecone.env").read_text() == env1, "the password is kept"
    assert (root / "etc/systemd/system/pinecone.service").read_text() == unit1
    assert "keeping the existing credential" in r.stdout
    assert "CREATE ROLE" not in r.stdout


@pytest.mark.parametrize("flag", ["--dry-run"])
def test_dry_run_changes_nothing(tmp_path: Path, flag: str) -> None:
    root = box(tmp_path)
    r = run(root, flag, "--yes")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "would:" in r.stdout
    assert not (root / "opt/pinecone").exists() and not (root / "etc/pinecone").exists()


def test_refuses_without_tak_server(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    r = run(root, "--yes")
    assert r.returncode == 2 and "TAK Server" in r.stdout + r.stderr


def test_the_address_the_operator_chose_survives_an_update(tmp_path: Path) -> None:
    """An update reconciles the services by re-running this installer with no arguments, so a bind
    that lived only in the unit file was reverted to loopback on the next release, taking Pinecone
    off the network with nothing said and nothing in the journal.

    Found by hunting the seam the previous fix created, before the review reached it. The direction
    of the failure is safe, which is exactly why it would have gone unnoticed.
    """
    root = box(tmp_path)
    unit = root / "etc/systemd/system/pinecone.service"

    def exec_line() -> str:
        return next(line for line in unit.read_text().splitlines() if line.startswith("ExecStart"))

    run(root, "--yes", "--bind", "10.0.0.5")
    assert "--bind 10.0.0.5" in exec_line()
    assert "PINECONE_BIND=10.0.0.5" in (root / "etc/pinecone/pinecone.env").read_text()

    run(root, "--yes")  # exactly what update.sh --reconcile does

    assert "--bind 10.0.0.5" in exec_line(), "an update does not quietly take the box off the network"

    run(root, "--yes", "--bind", "127.0.0.1")
    assert "--bind 127.0.0.1" in exec_line(), "and an explicit choice still wins"
    run(root, "--yes")
    assert "--bind 127.0.0.1" in exec_line(), "and is remembered in its turn"


def test_a_stored_address_that_is_not_an_address_is_said_so_not_ignored(tmp_path: Path) -> None:
    """Falling back in silence is the same fault the remembering exists to stop: an operator's own
    edit quietly not honoured, and a box that answers somewhere other than where they put it."""
    root = box(tmp_path)
    run(root, "--yes", "--bind", "10.0.0.5")
    env = root / "etc/pinecone/pinecone.env"
    env.write_text(env.read_text().replace("PINECONE_BIND=10.0.0.5", "PINECONE_BIND=pinecone.example.org"))

    r = run(root, "--yes")

    assert "WARN" in r.stderr and "not an IPv4 address" in r.stderr
    assert "pinecone.example.org" in r.stderr, "it names the value it could not use"
    unit = next(
        line
        for line in (root / "etc/systemd/system/pinecone.service").read_text().splitlines()
        if line.startswith("ExecStart")
    )
    assert "--bind 127.0.0.1" in unit, "and falls back to the default rather than to nothing"


def test_a_box_installed_before_the_address_was_remembered_keeps_its_address(tmp_path: Path) -> None:
    """The case the remembering exists for, and the one it missed.

    Every box in the field was laid down by a version that wrote the bind into the unit file and
    nowhere else. On the first update, the very update this mechanism makes safe, the environment
    file has nothing to remember, so a box deliberately bound off loopback would have left the
    network under output reading as success. The existing unit is the only place that choice lives
    on such a box, so it is read as the fallback.
    """
    root = box(tmp_path)
    run(root, "--yes", "--bind", "10.0.0.5", "--port", "9000")
    env = root / "etc/pinecone/pinecone.env"
    # Wind the box back to how a 0.3.0 install left it: the unit carries the choice, nothing else.
    kept = [line for line in env.read_text().splitlines() if not line.startswith("PINECONE_")]
    env.write_text("".join(line + "\n" for line in kept))
    assert "PINECONE_BIND" not in env.read_text()

    r = run(root, "--yes")  # exactly what an update does

    unit = next(
        line
        for line in (root / "etc/systemd/system/pinecone.service").read_text().splitlines()
        if line.startswith("ExecStart")
    )
    assert "--bind 10.0.0.5" in unit, "the box does not leave the network on its first update"
    assert "--port 9000" in unit
    assert "carried the address over" in r.stdout, "and it says that it did so"
    assert "PINECONE_BIND=10.0.0.5" in env.read_text(), "and it is remembered from then on"


def test_the_address_is_carried_over_from_a_directive_not_from_a_comment(tmp_path: Path) -> None:
    """An unanchored read takes a commented-out ExecStart above the live one, which is exactly how
    an operator leaves the address they have just moved away from. Carrying that one over would
    return the box to somewhere it was deliberately moved off."""
    root = box(tmp_path)
    run(root, "--yes", "--bind", "10.0.0.5")
    env = root / "etc/pinecone/pinecone.env"
    kept = [line for line in env.read_text().splitlines() if not line.startswith("PINECONE_")]
    env.write_text("".join(line + "\n" for line in kept))
    unit = root / "etc/systemd/system/pinecone.service"
    unit.write_text(
        "#ExecStart=/usr/bin/python3 /opt/pinecone/serve.py --bind 9.9.9.9 --port 9999\n" + unit.read_text()
    )

    run(root, "--yes")

    live = next(line for line in unit.read_text().splitlines() if line.startswith("ExecStart"))
    assert "--bind 10.0.0.5" in live, "the live directive is what is carried over"
    assert "9.9.9.9" not in live
    assert "PINECONE_BIND=10.0.0.5" in env.read_text()


def test_an_abandoned_address_is_not_carried_over_even_when_it_is_a_more_exposed_one(
    tmp_path: Path,
) -> None:
    """The severe shape of the same fault. An operator who once ran on every interface and later
    narrowed to one address, commenting the old line out rather than deleting it, would have been
    put back on 0.0.0.0 by the update, and the stale value written to the environment file so it
    stuck. Availability loss in the safe direction is one thing; silent re-exposure of the
    movements of identifiable people is another."""
    root = box(tmp_path)
    run(root, "--yes", "--bind", "10.0.0.5", "--port", "9000")
    env = root / "etc/pinecone/pinecone.env"
    kept = [line for line in env.read_text().splitlines() if not line.startswith("PINECONE_")]
    env.write_text("".join(line + "\n" for line in kept))
    unit = root / "etc/systemd/system/pinecone.service"
    unit.write_text(
        "#ExecStart=/usr/bin/python3 /opt/pinecone/serve.py --bind 0.0.0.0 --port 8765\n" + unit.read_text()
    )

    run(root, "--yes")

    live = next(line for line in unit.read_text().splitlines() if line.startswith("ExecStart"))
    assert "--bind 10.0.0.5" in live, "the address it was actually running on"
    # The suppression below is correct: this asserts the box is NOT bound to every interface.
    assert "0.0.0.0" not in live, "not the one it was moved off"  # noqa: S104
    assert "PINECONE_BIND=0.0.0.0" not in env.read_text(), "and the abandoned value is not made sticky"


def test_the_later_directive_wins_when_a_unit_carries_two(tmp_path: Path) -> None:
    root = box(tmp_path)
    run(root, "--yes", "--bind", "10.0.0.5")
    env = root / "etc/pinecone/pinecone.env"
    kept = [line for line in env.read_text().splitlines() if not line.startswith("PINECONE_")]
    env.write_text("".join(line + "\n" for line in kept))
    unit = root / "etc/systemd/system/pinecone.service"
    older = "ExecStart=/usr/bin/python3 /opt/pinecone/serve.py --bind 172.16.0.9 --port 8765\n"
    unit.write_text(older + unit.read_text())

    run(root, "--yes")

    live = [line for line in unit.read_text().splitlines() if line.startswith("ExecStart")]
    assert len(live) == 1
    assert "--bind 10.0.0.5" in live[0], "the later directive is the live one"


def test_the_choice_not_to_record_chat_survives_an_update(tmp_path: Path) -> None:
    """The same fault, a slice later. Spec 008 promised PINECONE_CHAT=no survived an update in four
    places and the installer was carrying forward only the address, the port and the backfill
    choice; the pre-UAT review of slice 5 ran the installer's own lines over an environment file
    holding the opt-out and watched it go. A setting the installer does not carry forward is a
    setting an update deletes, whatever the documents say."""
    root = box(tmp_path)
    run(root, "--yes")
    env = root / "etc/pinecone/pinecone.env"
    assert "PINECONE_CHAT=yes" in env.read_text(), "a fresh install records the default, so the knob is visible"

    env.write_text(env.read_text().replace("PINECONE_CHAT=yes", "PINECONE_CHAT=no"))
    run(root, "--yes")  # exactly what update.sh does after it has put the new files in place

    assert "PINECONE_CHAT=no" in env.read_text(), "the operator's choice is still there"
    assert "PINECONE_BACKFILL=yes" in env.read_text(), "and the other choices were not disturbed"


def test_the_role_can_read_the_chat_table_on_every_run(tmp_path: Path) -> None:
    """TAK Server 5.8 keeps GeoChat in cot_router_chat, so the role needs SELECT on it; and a box
    installed before this needs the grant on its next update, so the grants run on every install,
    not only when the role is created."""
    root = box(tmp_path)
    run(root, "--yes")
    sql = (root / "fake.log").read_text()
    assert "GRANT SELECT ON cot_router_chat TO pinecone" in sql
    (root / "fake.log").write_text("")
    run(root, "--yes", role_exists=True)  # the credential is kept; the grants still run
    sql = (root / "fake.log").read_text()
    assert "GRANT SELECT ON cot_router TO pinecone" in sql and "GRANT SELECT ON cot_router_chat TO pinecone" in sql
    assert "CREATE ROLE" not in sql


def test_the_choice_of_record_shape_survives_an_update(tmp_path: Path) -> None:
    """Spec 010, criterion 7. The record's shape is the third setting that lives in the environment
    file, and the third that the installer has to carry forward or an update deletes it."""
    root = box(tmp_path)
    run(root, "--yes")
    env = root / "etc/pinecone/pinecone.env"
    assert "PINECONE_RECORD=odcr" in env.read_text(), "a fresh install records the default, so the knob is visible"

    env.write_text(env.read_text().replace("PINECONE_RECORD=odcr", "PINECONE_RECORD=sustain-improve"))
    run(root, "--yes")

    assert "PINECONE_RECORD=sustain-improve" in env.read_text(), "the unit's own shape is still there"
    assert "PINECONE_CHAT=yes" in env.read_text(), "and the other choices were not disturbed"


def test_the_choice_not_to_take_the_history_survives_an_update(tmp_path: Path) -> None:
    """The installer rewrites the environment file wholesale on every run, and an update re-runs
    the installer, so a setting it does not carry forward is a setting an update deletes.

    Three documents promised this one survived an update. It did not, and the post-merge review of
    0.4.0 found it: the README, the recorder's own help text and the commit message all said so
    while the installer was truncating it away.
    """
    root = box(tmp_path)
    run(root, "--yes")
    env = root / "etc/pinecone/pinecone.env"
    assert "PINECONE_BACKFILL=yes" in env.read_text(), "a fresh install records the default"

    env.write_text(env.read_text().replace("PINECONE_BACKFILL=yes", "PINECONE_BACKFILL=no"))
    run(root, "--yes")  # exactly what update.sh --reconcile does

    assert "PINECONE_BACKFILL=no" in env.read_text(), "the operator's choice is still there"
    assert "PGPASSWORD=" in env.read_text(), "and the credential was not disturbed"
