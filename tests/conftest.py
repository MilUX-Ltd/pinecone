"""Shared fixtures. The suites import the flat scripts by path.

This file is the only place under tests/ that starts serve.py as a child. Thirteen fixtures across
ten files each carried their own copy of the start, wait and terminate code, each picking a port
from the process id; spec 012 makes them one helper, so a served fixture no longer picks a port
that something else can already be holding.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def root() -> Path:
    return ROOT


@pytest.fixture(scope="session")
def build_bundle() -> ModuleType:
    return load("build_bundle")


@pytest.fixture(scope="session")
def serve() -> ModuleType:
    return load("serve")


# ---- the one served fixture (spec 012) ---------------------------------------------------------

# What serve.py prints once it has bound. The port in it is the one it actually got, which is how
# a caller passing --port 0 learns which port to talk to.
BANNER = re.compile(r"Pinecone \S+: http://\S+:(\d+)/")

START_TIMEOUT = 30.0


@dataclass(frozen=True)
class Served:
    """A running Pinecone, the port it actually bound, and what it has said so far."""

    url: str
    port: int
    said: Callable[[], str]


class _Said:
    """Everything the child has printed, drained on a thread.

    Drained rather than left in the pipe, for two reasons. A pipe nobody reads fills and stops the
    child once it has logged enough page requests. And when the child dies, its own account of why
    is the only thing that explains the failure, so it has to have been kept.
    """

    def __init__(self, child: subprocess.Popen[str]) -> None:
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._read: list[str] = []
        self._child = child
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        assert self._child.stdout is not None
        for line in self._child.stdout:
            self._lines.put(line)
        self._lines.put(None)

    def next_line(self, timeout: float) -> str | None:
        """The next line the child printed, or None if it printed nothing in time or has finished."""
        try:
            line = self._lines.get(timeout=timeout)
        except queue.Empty:
            return None
        if line is None:
            return None
        self._read.append(line)
        return line

    def so_far(self) -> str:
        """Everything the child has printed and the pump has already queued, without waiting.

        `everything` waits for the pipe to close, which is right when quoting a child that has
        died and wrong while one is still serving. A test that wants to assert what a live server
        has put on its error stream needs this instead, or it pays the settle time on every call.
        """
        while True:
            try:
                line = self._lines.get_nowait()
            except queue.Empty:
                break
            if line is None:
                break
            self._read.append(line)
        return "".join(self._read)

    def everything(self, settle: float = 2.0) -> str:
        """Everything the child said, waiting briefly for its last words to arrive.

        A child that has just died is quoted in the failure, and the line worth quoting is usually
        the last one it managed. Reading only what the pump has already queued races it, so this
        waits for the pipe to close before giving up.
        """
        deadline = time.monotonic() + settle
        while not _past(deadline):
            try:
                line = self._lines.get(timeout=0.05)
            except queue.Empty:
                continue
            if line is None:
                break
            self._read.append(line)
        return "".join(self._read)


def _what_answered(port: int) -> str:
    """Whatever is on that port, in its own words.

    The failure this helper exists to remove was a stranger answering /version with 200 and the
    fixture taking that for readiness. So when the start fails, the stranger is named rather than
    left for the reader to find by hand.
    """
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/version", timeout=2) as reply:
            body = reply.read(2000).decode("utf-8", "replace")
        return f"something else is answering on port {port} and it is not this server: {body}"
    except urllib.error.HTTPError as e:
        return f"something else is answering on port {port} and it is not this server: HTTP {e.code}"
    except Exception as e:
        return f"nothing answered /version on port {port} ({e.__class__.__name__}: {e})"


def _failed(why: str, port: int | None, said: _Said) -> RuntimeError:
    """A failure that names the port, carries the child's own output, and says what answered.

    The port is None when the child never reached one and none was asked for, which is the default.
    Probing port 0 and calling it by that name would be noise in the one message whoever hits this
    failure is going to read.
    """
    named = f" on port {port}" if port else ""
    answered = f"{_what_answered(port)}\n" if port else ""
    return RuntimeError(
        f"serve.py did not come up{named}: {why}\n"
        f"{answered}"
        f"--- what serve.py said ---\n{said.everything() or '(it said nothing)'}"
    )


def _bound_port(child: subprocess.Popen[str], said: _Said, asked: int, deadline: float) -> int:
    """Read the port back from the child's own announcement, or fail with why it never came."""
    while True:
        line = said.next_line(timeout=0.2)
        if line is not None:
            found = BANNER.search(line)
            if found:
                return int(found.group(1))
        elif child.poll() is not None:
            raise _failed(f"it exited with code {child.returncode} before it was listening", asked or None, said)
        # Checked every time round, not only when the child is quiet: one that dies chatty would
        # otherwise keep this loop fed with lines and outlive its own deadline.
        if _past(deadline):
            raise _failed("it never announced an address", asked or None, said)


def _past(deadline: float) -> bool:
    return time.monotonic() > deadline


def _answers_as_pinecone(child: subprocess.Popen[str], said: _Said, port: int, deadline: float) -> None:
    """Wait until this server answers, and never take a stranger's 200 for readiness.

    The port came from the child's own banner, so it is the child's; the check that matters is that
    the child is still alive when the reply arrives. A reply from a port whose owner has died is
    somebody else's.
    """
    while not _past(deadline):
        if child.poll() is not None:
            raise _failed(f"it exited with code {child.returncode} before it answered", port, said)
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/version", timeout=1) as reply:
                raw = reply.read()
        except OSError:  # not listening yet, which is the normal case on the way up
            time.sleep(0.1)
            continue
        if child.poll() is not None:
            raise _failed("something answered but this server had already exited", port, said)
        try:
            answered = json.loads(raw.decode())
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise _failed(f"what answered /version is not Pinecone: {e}", port, said) from e
        if "repo" not in answered or "version" not in answered:
            raise _failed("what answered /version is not Pinecone", port, said)
        return
    raise _failed("it never answered /version", port, said)


@contextmanager
def _serve(
    data: Path | str,
    port: int = 0,
    env: dict[str, str] | None = None,
    args: Sequence[str] = (),
) -> Iterator[Served]:
    argv = [sys.executable, str(ROOT / "serve.py"), "--port", str(port), "--data", str(data), *args]
    child = subprocess.Popen(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    said = _Said(child)
    deadline = time.monotonic() + START_TIMEOUT
    try:
        bound = _bound_port(child, said, port, deadline)
        _answers_as_pinecone(child, said, bound, deadline)
        yield Served(url=f"http://127.0.0.1:{bound}", port=bound, said=said.so_far)
    finally:
        # Terminated, then killed if it will not go. A child left alive keeps its port and keeps the
        # reader thread blocked on the pipe for the rest of the session, which is exactly the kind of
        # stray process that caused this card.
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)
        if child.stdout is not None:
            child.stdout.close()


@pytest.fixture()
def serve_pinecone() -> Callable[..., contextlib.AbstractContextManager[Served]]:
    """Start Pinecone for a test, and hand back the port it actually bound.

        with serve_pinecone(data=tmp_path, args=("--maps", str(maps))) as served:
            ...  # served.url and served.port

    The port defaults to 0, so a fixture picks no port and cannot collide with anything. Pass an
    explicit port only to arrange a failure on purpose. A start that does not come up raises
    RuntimeError naming the port, carrying serve.py's own output and saying what answered instead.
    """
    return _serve


@pytest.fixture()
def env_no_git(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.setenv("PINECONE_REPO", "MilUX-Ltd/this-repository-does-not-exist")
    os.environ.pop("PINECONE_GITHUB_TOKEN", None)
