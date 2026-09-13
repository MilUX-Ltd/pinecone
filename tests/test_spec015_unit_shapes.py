"""Spec 015: what the old unit really says.

A box installed before 0.4.0 holds the operator's chosen address in its unit file and nowhere else,
so the first update reads it back out. These are the shapes of that file the read must understand,
and the one case where getting it wrong leaves a box exposed while reporting that it is not.

Run against a box in a directory: PINECONE_ROOT relocates every path and the fake commands in
tests/fakebin stand in for a real host. The live proof is a real box, and it is spec 015's
non-test criterion.

Eight of these were the failing acceptance criteria, marked xfail(strict=True) until the build
removed the mark. Three are guards that passed before the build too: one shape the card listed as
broken and which is not, and two drop-in properties that held vacuously while drop-ins were ignored
altogether and hold for a real reason now they are read. A guard marked strict-xfail would have
turned the suite red for the wrong reason, so each is marked as a guard instead and says why.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

FAKEBIN = Path(__file__).resolve().parent / "fakebin"
UNIT = "etc/systemd/system/pinecone.service"
DROPIN_DIR = "etc/systemd/system/pinecone.service.d"
LOGICAL_UNIT = "/etc/systemd/system/pinecone.service"


def box(tmp_path: Path) -> Path:
    """The same box-in-a-directory the installer's own suite builds."""
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
    return root


def run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PATH"] = f"{FAKEBIN}:{env['PATH']}"
    env["PINECONE_ROOT"] = str(root)
    env["PINECONE_FAKE_LOG"] = str(root / "fake.log")
    here = Path(__file__).resolve().parent.parent
    return subprocess.run(
        ["bash", str(here / "install.sh"), *args],
        cwd=here,
        env=env,
        input="",
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def wind_back_to_a_pre_0_4_0_box(root: Path, exec_start: str) -> None:
    """Leave the box exactly as a release before 0.4.0 left it: the unit carries the operator's
    choice and the environment file has never heard of it. Then write the unit in the shape under
    test, replacing the live ExecStart directive and leaving the rest of the file alone."""
    env = root / "etc/pinecone/pinecone.env"
    kept = [line for line in env.read_text().splitlines() if not line.startswith("PINECONE_")]
    env.write_text("".join(line + "\n" for line in kept))
    assert "PINECONE_BIND" not in env.read_text()

    unit = root / UNIT
    lines = unit.read_text().splitlines()
    out: list[str] = []
    replaced = False
    for line in lines:
        if line.startswith("ExecStart=") and "serve.py" in line:
            out.append(exec_start)
            replaced = True
        else:
            out.append(line)
    assert replaced, "the installed unit had no serve.py ExecStart to replace"
    unit.write_text("\n".join(out) + "\n")


def drop_in(root: Path, name: str, body: str) -> None:
    d = root / DROPIN_DIR
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(body)


def live_exec_start(root: Path) -> str:
    return next(line for line in (root / UNIT).read_text().splitlines() if line.startswith("ExecStart"))


def installed(tmp_path: Path, bind: str = "10.0.0.5", port: str = "9000") -> Path:
    root = box(tmp_path)
    r = run(root, "--yes", "--bind", bind, "--port", port)
    assert r.returncode == 0, r.stdout + r.stderr
    return root


# Criterion 1


def test_a_continued_line_is_one_command(tmp_path: Path) -> None:
    """systemd joins a line ending in a backslash with the one after it. The read looks at one line
    at a time, so the address is on a line it never considers and the box leaves the network."""
    root = installed(tmp_path)
    wind_back_to_a_pre_0_4_0_box(
        root,
        "ExecStart=/usr/bin/python3 /opt/pinecone/serve.py \\\n    --bind 10.0.0.5 --port 9000",
    )

    r = run(root, "--yes")

    assert r.returncode == 0, r.stdout + r.stderr
    assert "--bind 10.0.0.5" in live_exec_start(root)
    assert "--port 9000" in live_exec_start(root)
    assert "carried the address over" in r.stdout
    assert "PINECONE_BIND=10.0.0.5" in (root / "etc/pinecone/pinecone.env").read_text()


# Criterion 2


@pytest.mark.parametrize(
    ("label", "exec_start"),
    [
        pytest.param(
            "the interpreter path is quoted and holds a space",
            'ExecStart="/usr/local/my python/bin/python3" /opt/pinecone/serve.py --bind 10.0.0.5 --port 9000',
            id="quoted-interpreter-path",
            # No xfail: this one already works, and the card is wrong to list it as broken. The
            # expression's .* spans the quotation marks and the space, so the address is read. It
            # stays here as the guard that the tokeniser does not lose a shape that works today.
        ),
        pytest.param(
            "the script path is quoted",
            'ExecStart="/usr/bin/python3" "/opt/pinecone/serve.py" --bind 10.0.0.5 --port 9000',
            id="quoted-script-path",
        ),
        pytest.param(
            "the address itself is quoted",
            'ExecStart=/usr/bin/python3 /opt/pinecone/serve.py --bind "10.0.0.5" --port 9000',
            id="quoted-address",
        ),
    ],
)
def test_a_quoted_word_is_one_word(tmp_path: Path, label: str, exec_start: str) -> None:
    """systemd splits the value into words and honours quotation marks. A regular expression does
    not, so a quoted script path hides the address and a quoted address reaches the environment
    file wearing its quotation marks, where the check for an IPv4 address then refuses it."""
    root = installed(tmp_path)
    wind_back_to_a_pre_0_4_0_box(root, exec_start)

    r = run(root, "--yes")

    assert r.returncode == 0, label + "\n" + r.stdout + r.stderr
    assert "--bind 10.0.0.5" in live_exec_start(root), label
    env = (root / "etc/pinecone/pinecone.env").read_text()
    assert "PINECONE_BIND=10.0.0.5" in env, label
    assert '"' not in next(line for line in env.splitlines() if line.startswith("PINECONE_BIND=")), label


# Criterion 3


def test_the_equals_form_is_the_same_address(tmp_path: Path) -> None:
    root = installed(tmp_path)
    wind_back_to_a_pre_0_4_0_box(root, "ExecStart=/usr/bin/python3 /opt/pinecone/serve.py --bind=10.0.0.5 --port=9000")

    r = run(root, "--yes")

    assert r.returncode == 0, r.stdout + r.stderr
    assert "--bind 10.0.0.5" in live_exec_start(root)
    assert "--port 9000" in live_exec_start(root)
    assert "PINECONE_BIND=10.0.0.5" in (root / "etc/pinecone/pinecone.env").read_text()


def test_a_tab_is_a_separator_like_a_space(tmp_path: Path) -> None:
    """Not named on the card, found by running the expression rather than reading it. systemd
    accepts a tab between words; the expression requires a space."""
    root = installed(tmp_path)
    wind_back_to_a_pre_0_4_0_box(
        root, "ExecStart=/usr/bin/python3 /opt/pinecone/serve.py\t--bind\t10.0.0.5\t--port\t9000"
    )

    r = run(root, "--yes")

    assert r.returncode == 0, r.stdout + r.stderr
    assert "--bind 10.0.0.5" in live_exec_start(root)
    assert "PINECONE_BIND=10.0.0.5" in (root / "etc/pinecone/pinecone.env").read_text()


# Criterion 4


def test_a_drop_in_is_what_the_box_is_running(tmp_path: Path) -> None:
    """The one shape that fails in the unsafe direction. The installer rewrites the unit file on
    every run and never touches the drop-in directory, so a drop-in that exposes the box survives
    the update and goes on overriding ExecStart, while the new unit says loopback and the closing
    line tells the operator they are on loopback. They are not."""
    root = installed(tmp_path, bind="127.0.0.1", port="8765")
    wind_back_to_a_pre_0_4_0_box(root, "ExecStart=/usr/bin/python3 /opt/pinecone/serve.py --bind 127.0.0.1 --port 8765")
    drop_in(
        root,
        "10-expose.conf",
        "[Service]\nExecStart=\nExecStart=/usr/bin/python3 /opt/pinecone/serve.py --bind 0.0.0.0 --port 8765\n",
    )

    r = run(root, "--yes")

    assert r.returncode == 0, r.stdout + r.stderr
    # The suppression is correct: this asserts the box reports the exposure it actually has.
    assert "--bind 0.0.0.0" in live_exec_start(root), "the address the box is really answering on"
    assert "PINECONE_BIND=0.0.0.0" in (root / "etc/pinecone/pinecone.env").read_text()
    closing = [line for line in r.stdout.strip().splitlines() if line.strip()][-2:]
    assert any("reachable from the network" in line for line in closing), "and says so on the way past"
    assert not any("loopback only" in line for line in closing), "it never claims loopback while a drop-in exposes it"


# Criterion 5


def test_the_last_drop_in_wins(tmp_path: Path) -> None:
    """systemd applies drop-ins in filename order, so the later file is the live configuration."""
    root = installed(tmp_path, bind="127.0.0.1", port="8765")
    wind_back_to_a_pre_0_4_0_box(root, "ExecStart=/usr/bin/python3 /opt/pinecone/serve.py --bind 127.0.0.1 --port 8765")
    drop_in(
        root,
        "10-a.conf",
        "[Service]\nExecStart=\nExecStart=/usr/bin/python3 /opt/pinecone/serve.py --bind 172.16.0.9 --port 8765\n",
    )
    drop_in(
        root,
        "20-b.conf",
        "[Service]\nExecStart=\nExecStart=/usr/bin/python3 /opt/pinecone/serve.py --bind 10.0.0.5 --port 8765\n",
    )

    r = run(root, "--yes")

    assert r.returncode == 0, r.stdout + r.stderr
    assert "--bind 10.0.0.5" in live_exec_start(root), "the later drop-in is the live one"
    assert "172.16.0.9" not in live_exec_start(root)


def test_a_drop_in_that_sets_no_exec_start_changes_nothing(tmp_path: Path) -> None:
    """Most drop-ins tune something else. One that does not touch ExecStart must leave the unit's
    own address alone rather than resetting it to nothing.

    A guard, not a failing criterion. It passes today for a reason that does not survive the fix:
    drop-ins are not read at all, so of course this one changes nothing. Once they are read it
    becomes the assertion that the reader distinguishes a drop-in that resets ExecStart from one
    that does not, which is the way a careless implementation would blank the address."""
    root = installed(tmp_path)
    wind_back_to_a_pre_0_4_0_box(root, "ExecStart=/usr/bin/python3 /opt/pinecone/serve.py --bind 10.0.0.5 --port 9000")
    drop_in(root, "10-restart.conf", "[Service]\nRestart=always\nRestartSec=5\n")

    r = run(root, "--yes")

    assert r.returncode == 0, r.stdout + r.stderr
    assert "--bind 10.0.0.5" in live_exec_start(root)
    assert "PINECONE_BIND=10.0.0.5" in (root / "etc/pinecone/pinecone.env").read_text()


# Criterion 6


def test_an_address_that_cannot_be_read_is_said_out_loud(tmp_path: Path) -> None:
    """The honest unknown. A unit whose arguments are hidden behind a variable cannot be resolved
    without expanding it, which this reader deliberately does not do. Falling silently to loopback
    and then printing loopback as though it were the operator's setting is the fault; saying which
    file could not be read is the fix. The closing lines are unchanged."""
    root = installed(tmp_path)
    wind_back_to_a_pre_0_4_0_box(root, "ExecStart=/usr/bin/python3 /opt/pinecone/serve.py $PINECONE_ARGS")

    r = run(root, "--yes")

    assert r.returncode == 0, r.stdout + r.stderr
    both = r.stdout + r.stderr
    assert "could not read" in both, "it says it could not read the address"
    assert LOGICAL_UNIT in both, "and names the file it could not read"
    assert "carried the address over" not in r.stdout, "and does not claim it carried anything over"
    assert "--bind 127.0.0.1" in live_exec_start(root), "the box is set to loopback, which is the safe default"
    closing = [line for line in r.stdout.strip().splitlines() if line.strip()][-2:]
    assert any(
        "loopback" in line.lower() and "no authentication" in line.lower() for line in closing
    ), "the existing closing line is untouched by this card"


# Criterion 7


def test_a_commented_out_drop_in_directive_is_not_read(tmp_path: Path) -> None:
    """The guard that carries threat note 003's anchoring lesson into the drop-in directory. A
    commented-out ExecStart is exactly how an operator leaves an address they have moved off, and
    reading one out of a drop-in would put the box back on it.

    A guard, not a failing criterion, and it passes today only because drop-ins are not read at
    all. It earns its place the moment they are: an implementation that greps a drop-in for
    ExecStart rather than parsing it fails here, and fails in the direction that puts the movements
    of identifiable people back on every interface."""
    root = installed(tmp_path, bind="127.0.0.1", port="8765")
    wind_back_to_a_pre_0_4_0_box(root, "ExecStart=/usr/bin/python3 /opt/pinecone/serve.py --bind 127.0.0.1 --port 8765")
    drop_in(
        root,
        "10-old.conf",
        "[Service]\n#ExecStart=\n#ExecStart=/usr/bin/python3 /opt/pinecone/serve.py --bind 0.0.0.0 --port 8765\n",
    )

    r = run(root, "--yes")

    assert r.returncode == 0, r.stdout + r.stderr
    live = live_exec_start(root)
    assert "--bind 127.0.0.1" in live, "the address the box is actually on"
    # The suppression is correct: this asserts the box is NOT put on every interface.
    assert "0.0.0.0" not in live, "not the one that was commented out"  # noqa: S104
    assert "PINECONE_BIND=0.0.0.0" not in (root / "etc/pinecone/pinecone.env").read_text()
