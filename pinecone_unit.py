#!/usr/bin/env python3
"""What address a systemd unit is really running with (Spec 015).

A box installed before 0.4.0 recorded the operator's chosen address in its unit file and nowhere
else, so the first update to 0.4.0 reads it back out. That read used to be two `sed` expressions,
which is a regular expression being asked to do a shell's word splitting. It missed a line
continued with a backslash, `--bind=`, a tab separator and a quoted script path; it kept the
quotation marks on a quoted address, so the IPv4 check then refused it; and it never looked in the
drop-in directory at all.

The drop-in is the one that matters. `install.sh` rewrites `pinecone.service` on every run and
never touches `pinecone.service.d/`, so a drop-in that puts the box on every interface survives the
update and goes on overriding ExecStart, while the freshly written unit says loopback and the
closing line tells the operator they are on loopback. Every other shape fails safe and says so;
this one fails silent and exposed.

One tokeniser, not seven patterns. `shlex` splits the way systemd does for the common shapes, so
the shapes the card listed and the two it missed all fall out of the same call.

What this deliberately does not do, because doing it badly is worse than not doing it: specifier
expansion (%i, %H), Environment= and EnvironmentFile= lookup, C-style escapes (\\x20), and the
other Exec* directives. A command whose arguments are hidden behind a variable is reported as
unreadable rather than guessed at. Drop-ins outside /etc/systemd/system/<unit>.d/ are not read;
systemd also reads /run and /usr/lib, and that is a residual limit recorded in threat note 015.

Prints nothing when the unit does not run serve.py. Otherwise one key=value line per fact:

    bind=10.0.0.5
    port=9000
    source=/etc/systemd/system/pinecone.service.d/10-expose.conf
    unresolved=/etc/systemd/system/pinecone.service
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

# systemd lets an executable be prefixed to change how it is run (@ argv[0], - ignore failure,
# : no variable expansion, + ! !! privilege). They are not part of the path.
EXEC_PREFIXES = "-@:+!"


def _logical_lines(text: str) -> list[str]:
    """systemd joins a line ending in a backslash with the one after it, so the address can sit on
    a line the old read never considered."""
    out: list[str] = []
    buf = ""
    for raw in text.splitlines():
        line = buf + raw
        buf = ""
        if line.endswith("\\"):
            buf = line[:-1] + " "
            continue
        out.append(line)
    if buf:
        out.append(buf)
    return out


def _exec_start_values(path: Path) -> list[str]:
    """Every ExecStart= this file sets, in order, including the empty one that resets the list.

    Anchored to a real directive. A commented-out ExecStart is exactly how an operator leaves an
    address they have just moved off, and reading one would put the box back on it.
    """
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return []
    values: list[str] = []
    for line in _logical_lines(text):
        stripped = line.lstrip()
        if stripped.startswith(("#", ";")):
            continue
        name, sep, value = stripped.partition("=")
        if sep and name.strip() == "ExecStart":
            values.append(value.strip())
    return values


def effective_exec_start(unit: Path, dropin_dir: Path | None = None) -> tuple[str, Path] | None:
    """The ExecStart the box is actually running, and the file it came from.

    Drop-ins are applied in filename order, which is the order systemd applies them, so the later
    file wins. An empty ExecStart= clears what came before, which is how a drop-in replaces the
    command rather than adding a second one; a drop-in that sets no ExecStart at all leaves the
    unit's own alone.
    """
    if dropin_dir is None:
        dropin_dir = unit.with_name(unit.name + ".d")
    files = [unit]
    if dropin_dir.is_dir():
        files += sorted(p for p in dropin_dir.iterdir() if p.name.endswith(".conf"))

    live: list[tuple[str, Path]] = []
    for f in files:
        for value in _exec_start_values(f):
            if value == "":
                live.clear()
            else:
                live.append((value, f))
    return live[-1] if live else None


def read(unit: Path, dropin_dir: Path | None = None) -> dict[str, str]:
    """The address and port the unit really carries, or a note that they cannot be read."""
    found = effective_exec_start(unit, dropin_dir)
    if found is None:
        return {}
    value, source = found

    try:
        words = shlex.split(value)
    except ValueError:
        # An unbalanced quotation mark. systemd would refuse the unit; we refuse to guess.
        return {"unresolved": str(source)}
    if words:
        words[0] = words[0].lstrip(EXEC_PREFIXES)
    if not any(w.endswith("serve.py") for w in words):
        return {}

    out: dict[str, str] = {}
    for i, w in enumerate(words):
        for flag in ("--bind", "--port"):
            key = flag[2:]
            if w == flag and i + 1 < len(words):
                out[key] = words[i + 1]
            elif w.startswith(flag + "="):
                out[key] = w[len(flag) + 1 :]

    # Arguments behind a variable cannot be resolved without expanding the unit's environment,
    # which this reader does not do. Saying so is the point: falling silently to loopback and then
    # printing loopback as the operator's own setting is the fault this card exists to fix.
    if "bind" not in out and any("$" in w for w in words):
        return {"unresolved": str(source)}

    if out:
        out["source"] = str(source)
    return out


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: pinecone_unit.py <unit file> [drop-in directory]", file=sys.stderr)
        return 2
    unit = Path(argv[0])
    dropin = Path(argv[1]) if len(argv) > 1 else None
    for k, v in read(unit, dropin).items():
        print(f"{k}={v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
