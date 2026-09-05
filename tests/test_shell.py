"""The shell scripts: they parse, they refuse politely, and they degrade without a network."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def sh(*args: str, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    import os

    e = dict(os.environ)
    if env:
        e.update(env)
    return subprocess.run(list(args), cwd=cwd, capture_output=True, text=True, env=e, timeout=60, check=False)


@pytest.mark.parametrize("script", ["pull.sh", "update.sh", "bin/ship.sh", "bin/package.sh", ".githooks/pre-commit"])
def test_scripts_parse(root: Path, script: str) -> None:
    assert sh("bash", "-n", script, cwd=root).returncode == 0


def test_pull_refuses_without_a_host_or_local_flag(root: Path) -> None:
    r = sh("./pull.sh", "2026-09-03T00:00Z", "2026-09-04T00:00Z", cwd=root, env={"PINECONE_HOST": ""})
    assert r.returncode == 2 and "usage" in r.stderr


def test_ship_refuses_without_the_baseline_word(root: Path) -> None:
    r = sh("bin/ship.sh", cwd=root)
    assert r.returncode == 2 and "refusing" in r.stdout


def test_update_check_reports_current_and_degrades_when_it_cannot_reach_github(root: Path, env_no_git: None) -> None:
    r = sh("./update.sh", "--check", cwd=root)
    assert "current=" in r.stdout
    assert r.returncode in (0, 3), r.stdout + r.stderr
    if r.returncode == 3:
        assert "error=cannot reach GitHub" in r.stdout


def test_update_apply_refuses_a_git_checkout(root: Path, env_no_git: None) -> None:
    r = sh("./update.sh", cwd=root)
    assert r.returncode == 2 and "git checkout" in r.stdout


def test_package_builds_the_public_snapshot_without_private_files(root: Path, tmp_path: Path) -> None:
    r = sh("bin/package.sh", str(tmp_path), cwd=root)
    assert r.returncode == 0, r.stdout + r.stderr
    tgz = next(tmp_path.glob("pinecone-*.tgz"))
    listing = subprocess.run(["tar", "-tzf", str(tgz)], capture_output=True, text=True, check=True).stdout
    names = {line.split("/", 1)[1] for line in listing.splitlines() if "/" in line}
    for private in (
        "CLAUDE.md",
        "CONTEXT.md",
        "CONTRIBUTING.md",
        "LESSONS.md",
        "bin/ship.sh",
        "docs/specs/README.md",
        "docs/security/threat-model.md",
        "docs/adr/001-target-architecture.md",  # the decision records stay private (decided 5 September 2026)
        "tests/test_repo_hygiene.py",  # a maintainer's test over the private tree; it carries the words it keeps out
    ):
        assert private not in names, private
    for public in (
        "README.md",
        "LICENSE",
        "SECURITY.md",
        "VERSION",
        "serve.py",
        "update.sh",
        "data/synthetic.json",
    ):
        assert public in names, public
    assert (tmp_path / (tgz.name + ".sha256")).read_text().split()[1].endswith(tgz.name)


def _box(root: Path, tmp_path: Path, installer: str) -> tuple[Path, Path]:
    """A directory standing in for an installed copy, with a stand-in installer."""
    import shutil

    box = tmp_path / "opt-pinecone"
    box.mkdir()
    shutil.copy(root / "update.sh", box / "update.sh")
    (box / "VERSION").write_text("0.4.0\n")
    (box / "install.sh").write_text(installer)
    (box / "install.sh").chmod(0o755)
    fakeroot = tmp_path / "fakeroot"
    (fakeroot / "etc/systemd/system").mkdir(parents=True)
    return box, fakeroot


def _reconcile(root: Path, box: Path, fakeroot: Path, log: Path | None = None) -> subprocess.CompletedProcess[str]:
    import os

    e = dict(os.environ)
    e["PATH"] = f"{root / 'tests' / 'fakebin'}:{e['PATH']}"
    e["PINECONE_ROOT"] = str(fakeroot)
    if log is not None:
        e["PINECONE_FAKE_LOG"] = str(log)
    return subprocess.run(
        ["./update.sh", "--reconcile"], cwd=box, capture_output=True, text=True, env=e, timeout=60, check=False
    )


WRITES_THE_UNIT = (
    "#!/usr/bin/env bash\n"
    'echo "install.sh $*" >> "${PINECONE_FAKE_LOG:-/dev/null}"\n'
    'touch "$PINECONE_ROOT/etc/systemd/system/pinecone-recorder.service"\n'
)

# A stand-in installer that succeeds while saying the things an operator must not miss.
WRITES_THE_UNIT_AND_TALKS = (
    "#!/usr/bin/env bash\n"
    'touch "$PINECONE_ROOT/etc/systemd/system/pinecone-recorder.service"\n'
    'echo "12:00:00 carried the address over from /etc/systemd/system/pinecone.service (10.0.0.5:9000)"\n'
    'echo "WARN the port in /etc/pinecone/pinecone.env is not a port number (nine thousand); using 8765" >&2\n'
    'echo "Bound to 10.0.0.5:9000, reachable from the network, with no authentication."\n'
    'echo "12:00:01 some ordinary progress line nobody needs"\n'
)


def test_an_update_reconciles_the_services_rather_than_only_restarting_the_old_one(root: Path, tmp_path: Path) -> None:
    """A release can add a service. 0.4.0 adds the recorder, and an update that restarted only the
    services the box already had would take the new files, report success, and run without it.

    Found by the third pre-UAT review of slice 1a: the outcome was a box with no record at all,
    which is the failure the product exists to prevent.
    """
    box, fakeroot = _box(root, tmp_path, WRITES_THE_UNIT)
    log = tmp_path / "calls.log"
    log.write_text("")

    r = _reconcile(root, box, fakeroot, log)

    assert r.returncode == 0, r.stdout + r.stderr
    assert "install.sh --yes" in log.read_text(), "the services are applied, not merely restarted"
    assert "services=reconciled" in r.stdout
    assert "pinecone-recorder.service" in r.stdout, "the recorder's state is reported, not assumed"


def test_a_reconcile_that_leaves_no_recorder_unit_refuses_to_call_itself_done(root: Path, tmp_path: Path) -> None:
    """An exit status is not evidence that the unit this step exists for was written. A box with
    the new code, a happy exit and no recorder is the exact failure being fixed."""
    box, fakeroot = _box(root, tmp_path, "#!/usr/bin/env bash\nexit 0\n")

    r = _reconcile(root, box, fakeroot)

    assert r.returncode == 5
    assert "wrote no pinecone-recorder.service" in r.stdout
    assert "services=reconciled" not in r.stdout


def test_an_update_that_cannot_apply_the_services_says_so_instead_of_claiming_success(
    root: Path, tmp_path: Path
) -> None:
    box, fakeroot = _box(root, tmp_path, "#!/usr/bin/env bash\necho 'ERR PostgreSQL did not answer' >&2\nexit 9\n")

    r = _reconcile(root, box, fakeroot)

    assert r.returncode == 5
    assert "error=install.sh could not apply the services" in r.stdout
    assert "PostgreSQL did not answer" in r.stdout, "the installer's own reason is passed on, not swallowed"
    assert "result=running" not in r.stdout, "a failed reconcile never reports a running version"


@pytest.mark.parametrize("kind", ["directory", "file"])
def test_reconcile_refuses_to_put_a_branch_on_a_box(root: Path, tmp_path: Path, kind: str) -> None:
    """--reconcile runs the installer against the copy in its own directory, so in a checkout it
    would lay a working tree over /opt/pinecone. Only a tagged release is ever put on a box.

    Both shapes of `.git`: a directory in a clone, a file in a worktree, which is how this repo is
    actually worked and therefore the shape most likely to be met."""
    box, fakeroot = _box(root, tmp_path, WRITES_THE_UNIT)
    if kind == "directory":
        (box / ".git").mkdir()
    else:
        (box / ".git").write_text("gitdir: /somewhere/else/.git/worktrees/x\n")

    r = _reconcile(root, box, fakeroot)

    assert r.returncode == 2
    assert "this is a git checkout" in r.stdout
    assert "services=reconciled" not in r.stdout


def test_an_update_that_worked_still_says_where_it_left_the_box(root: Path, tmp_path: Path) -> None:
    """The safety net for everything the migration cannot parse is a warning, and on the update
    path nobody saw it: install.sh's output went to /dev/null and its stderr was printed only when
    it failed. So an update could move where the box answers, or put it back on every interface,
    and say nothing at all. The lines that matter are shown even when it worked; the ordinary
    progress lines are not."""
    box, fakeroot = _box(root, tmp_path, WRITES_THE_UNIT_AND_TALKS)

    r = _reconcile(root, box, fakeroot)

    assert r.returncode == 0, r.stdout + r.stderr
    assert "carried the address over" in r.stdout, "an address taken from the old unit is announced"
    assert "WARN the port" in r.stdout, "and so is a value it could not use"
    assert "Bound to 10.0.0.5:9000, reachable from the network" in r.stdout, "and where it left the box"
    assert "ordinary progress line" not in r.stdout, "without repeating the whole install"
