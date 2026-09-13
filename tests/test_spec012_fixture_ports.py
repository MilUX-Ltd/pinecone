"""Spec 012: a fixture's port collision fails loudly, or cannot happen.

Committed failing, before the build, per CONTRIBUTING.md. Every test here carries
xfail(strict=True) until the build commit removes the mark, and that removal is the proof the
test went from red to green.

The helper these criteria are written against does not exist yet. It is resolved inside each
test body rather than taken as a fixture argument, because a missing fixture in the signature
is a setup error, and a setup error is not an expected failure: it would turn the run red on
the commit that is meant to keep it green.
"""

from __future__ import annotations

import ast
import json
import queue
import re
import socket
import subprocess
import sys
import threading
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

NOT_BUILT = "Spec 012, not built"

# The name the consolidated helper is published under in tests/conftest.py. Criterion 2 asserts
# every served fixture asks for it, so it is part of the spec's contract, not a preference.
HELPER = "serve_pinecone"

# The thirteen served fixtures that each carry their own copy of the start, wait and terminate
# code today, across the ten files that hold them. Counted from the tree on 6 September 2026:
# spec 001, 002 and 003 hold two each, the rest one apiece.
SERVED_FIXTURE_FILES = (
    "test_spec001_page.py",
    "test_spec002_map_page.py",
    "test_spec003_archive_api.py",
    "test_spec004_window.py",
    "test_spec005_overlays_api.py",
    "test_spec006_moments.py",
    "test_spec007_time.py",
    "test_spec009_proposals.py",
    "test_spec010_record.py",
    "test_spec011_map_asks.py",
)

# Every way a test could start a child. Criterion 2 says none of them may run serve.py outside
# conftest.py, whether by subprocess, by module invocation or by shell.
SPAWNING_CALLS = frozenset(
    {
        "Popen",
        "run",
        "call",
        "check_call",
        "check_output",
        "getoutput",
        "getstatusoutput",
        "system",
        "popen",
        "spawnv",
        "spawnvp",
        "execv",
        "execvp",
        "execvpe",
    }
)

# What the stranger says about itself, so a failure can be checked for naming what answered.
STRANGER = "not-pinecone-a-stranger-on-this-port"

BANNER = re.compile(r"http://127\.0\.0\.1:(\d+)/")


def called_name(func: ast.expr) -> str:
    """The bare name of whatever is being called, so subprocess.Popen and Popen both read as Popen."""
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def builds_the_path_to_serve_py(node: ast.AST) -> bool:
    """True for ROOT / "serve.py" and os.path.join(ROOT, "serve.py").

    This is the signal that a file means to run the repository's own server, as against naming
    serve.py in a docstring, in a list of filenames, or in an assertion about an installed tree
    where the path is opt/pinecone/serve.py and not serve.py on its own.
    """
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        right = node.right
        return isinstance(right, ast.Constant) and right.value == "serve.py"
    if isinstance(node, ast.Call) and called_name(node.func) == "join":
        return any(isinstance(arg, ast.Constant) and arg.value == "serve.py" for arg in node.args)
    return False


def starts_serve_py(source: str) -> bool:
    """True when this source starts serve.py as a child by any route.

    Two nets, because one is not enough. The first catches a spawning call that names the script
    in its own arguments. The second catches the case the first misses, where the argv is built
    into a variable first and handed to the call afterwards, which is how one of the thirteen
    fixtures is written today. A file may name serve.py in a string, a docstring or an assertion
    about an installed tree and be innocent, so neither net fires on the name alone.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if builds_the_path_to_serve_py(node):
            return True
        if isinstance(node, ast.Call) and called_name(node.func) in SPAWNING_CALLS:
            segment = ast.get_source_segment(source, node) or ""
            if "serve.py" in segment:
                return True
    return False


@contextmanager
def a_held_port() -> Iterator[int]:
    """A port this process holds open for the duration, so a child asking for it cannot have it."""
    held = socket.socket()
    held.bind(("127.0.0.1", 0))
    held.listen(1)
    try:
        yield int(held.getsockname()[1])
    finally:
        held.close()


class StrangerHandler(BaseHTTPRequestHandler):
    """Something that is not Pinecone, answering /version with 200 as a real stranger did."""

    def do_GET(self) -> None:
        body = json.dumps({"version": STRANGER, "repo": STRANGER}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Server", STRANGER)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        return


@contextmanager
def a_stranger_on_a_port() -> Iterator[int]:
    """A server that is not Pinecone, holding a port and answering /version with 200."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), StrangerHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(httpd.server_address[1])
    finally:
        httpd.shutdown()
        httpd.server_close()


def lines_until(
    child: subprocess.Popen[str], pattern: re.Pattern[str], timeout: float = 20.0
) -> tuple[str, str | None]:
    """Read the child's output until the pattern matches or it stops, and never block for ever.

    Returns everything read and the first captured group, or None if the pattern never matched.
    The child's stdout is a pipe nobody reads today, which is half of why this card exists, so
    the reading is done on a thread and the wait is bounded.
    """
    seen: queue.Queue[str | None] = queue.Queue()

    def pump() -> None:
        assert child.stdout is not None
        for line in child.stdout:
            seen.put(line)
        seen.put(None)

    threading.Thread(target=pump, daemon=True).start()
    read = ""
    deadline = threading.Event()
    timer = threading.Timer(timeout, deadline.set)
    timer.start()
    try:
        while not deadline.is_set():
            try:
                line = seen.get(timeout=0.2)
            except queue.Empty:
                continue
            if line is None:
                break
            read += line
            found = pattern.search(line)
            if found:
                return read, found.group(1)
    finally:
        timer.cancel()
    return read, None


def test_a_child_that_dies_fails_the_fixture_with_its_own_output(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    """Criterion 1. The helper raises at once, names the port, and shows what the child said."""
    serve_pinecone = request.getfixturevalue(HELPER)
    with a_held_port() as taken:
        with pytest.raises(RuntimeError) as failure, serve_pinecone(data=tmp_path, port=taken) as served:
            pytest.fail(f"the helper handed back {served} against a port it could not have")
        said = str(failure.value)
    assert str(taken) in said, f"the failure never names the port: {said}"
    assert "Address already in use" in said, f"the failure does not carry the child's own output: {said}"


def test_every_served_fixture_comes_from_the_helper(root: Path) -> None:
    """Criterion 2. One helper, thirteen call sites, and nothing left picking a port."""
    # This file skips itself, the way tests/test_repo_hygiene.py does, and for the same two
    # reasons: it writes the very token it looks for, and criterion 4 has to start serve.py
    # directly to watch what it announces. Nothing else under tests/ may do either. The
    # exemption is not a way in for a fixture, because the builder never edits an acceptance
    # test once it is committed; changing one is a decision request.
    mine = Path(__file__).name
    sources = {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted((root / "tests").glob("*.py"))
        if path.name != mine
    }

    still_picking = sorted(name for name, text in sources.items() if "getpid()" in text)
    assert not still_picking, f"these still derive a port from the process id: {still_picking}"

    starting_it_themselves = sorted(
        name for name, text in sources.items() if name != "conftest.py" and starts_serve_py(text)
    )
    assert not starting_it_themselves, f"these start serve.py rather than asking the helper: {starting_it_themselves}"

    assert HELPER in sources["conftest.py"], f"conftest.py publishes no {HELPER} helper"

    never_asking = sorted(name for name in SERVED_FIXTURE_FILES if HELPER not in sources.get(name, ""))
    assert not never_asking, f"these served fixtures never ask the helper for a server: {never_asking}"


def test_a_stranger_answering_version_is_not_mistaken_for_the_server(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    """Criterion 3. A 200 from /version is not readiness unless Pinecone is what said it."""
    serve_pinecone = request.getfixturevalue(HELPER)
    with a_stranger_on_a_port() as taken:
        with pytest.raises(RuntimeError) as failure, serve_pinecone(data=tmp_path, port=taken) as served:
            pytest.fail(f"the helper handed back {served}, which is the stranger, not Pinecone")
        said = str(failure.value)
    assert str(taken) in said, f"the failure never names the port: {said}"
    assert STRANGER in said, f"the failure does not say what answered: {said}"


def test_the_server_announces_the_port_it_actually_bound(root: Path, tmp_path: Path) -> None:
    """Criterion 4. With --port 0 the announced address is the real one, and it never claims to
    serve before it has bound. The second half is the check anyone can run by hand in seconds."""
    child = subprocess.Popen(
        [sys.executable, str(root / "serve.py"), "--port", "0", "--data", str(tmp_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        read, announced = lines_until(child, BANNER)
        assert announced is not None, f"the server never announced an address: {read}"
        assert announced != "0", f"the server announced the port it was asked for, not the one it bound: {read}"
        with urllib.request.urlopen(f"http://127.0.0.1:{announced}/version", timeout=5) as reply:
            assert reply.status == 200
            assert json.loads(reply.read().decode())["repo"], "the announced port answers, but not as Pinecone"
    finally:
        child.terminate()
        child.wait(timeout=5)

    with a_held_port() as taken:
        refused = subprocess.run(
            [sys.executable, str(root / "serve.py"), "--port", str(taken), "--data", str(tmp_path)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    said = refused.stdout + refused.stderr
    assert refused.returncode != 0, f"the server survived a port it cannot have: {said}"
    assert "Address already in use" in said, f"the server did not say why it stopped: {said}"
    assert "Serving." not in said, f"the server announced that it was serving and then failed: {said}"
