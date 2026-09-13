"""Spec 013, one place reads a request body: a malformed length is answered, never awaited.

Everything here is synthetic. No real client, exercise or estate data.

All four criteria were committed as strict expected failures at 24728e8, each run against the
unfixed server and shown to fail for its own reason rather than incidentally. The fix landed, so
the four marks are gone and the criteria are simply asserted. No assertion has moved since.
"""

from __future__ import annotations

import ast
import inspect
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import NamedTuple

import pytest

ROOT = Path(__file__).resolve().parent.parent

# The three routes that read a body through min(length, cap) and trust it. records_post is the
# fourth site that reads a body and is deliberately absent: it already answers 400 on both faults
# by its own shape, and spec 013 names these three as the scope.
ROUTES = ["/api/moments", "/api/packs/import", "/api/maps/choose"]

# The helper the fix introduces on H, and the single route allowed to keep a read of its own.
HELPER = "_read_body"
ALLOWED_TO_READ_THE_SOCKET = {HELPER, "records_post"}


class Reply(NamedTuple):
    """What came back, and if nothing did, which of the two ways it went wrong.

    The distinction matters: the two faults in this card look identical to a caller that only
    records "no status". A negative length holds the handler until we give up, and a non-numeric
    one drops the connection at once with nothing in it. A test that could not tell them apart
    would report both criteria failing for the same reason and prove neither.
    """

    status: int | None
    body: str
    elapsed: float
    outcome: str


ANSWERED = "answered"
CLOSED_EMPTY = "closed the connection without answering"
NEVER_ANSWERED = "held the connection open and never answered"


def raw_post(
    port: int,
    path: str,
    length_header: str,
    payload: bytes = b"at=1&name=x",
    timeout: float = 3.0,
) -> Reply:
    """POST with a chosen Content-Length, and read whatever comes back.

    urllib will not send a length it knows to be wrong, so the request is written by hand. The
    socket is held open until we give up, deliberately: a negative length makes the server read to
    end of file, so closing early would hide the very hang we are trying to observe.
    """
    request = (
        f"POST {path} HTTP/1.0\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        "Content-Type: application/x-www-form-urlencoded\r\n"
        f"Content-Length: {length_header}\r\n"
        "\r\n"
    ).encode() + payload
    started = time.monotonic()
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        s.sendall(request)
        s.settimeout(timeout)
        raw = b""
        try:
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                raw += chunk
        except (TimeoutError, OSError):  # socket.timeout is an alias of TimeoutError
            return Reply(None, "", time.monotonic() - started, NEVER_ANSWERED)
    elapsed = time.monotonic() - started
    if not raw:
        return Reply(None, "", elapsed, CLOSED_EMPTY)
    head, _, body = raw.partition(b"\r\n\r\n")
    return Reply(int(head.split(b" ")[1]), body.decode("utf-8", "replace"), elapsed, ANSWERED)


def get_status(port: int, path: str, timeout: float = 5.0) -> int:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def reads_of_the_request_socket(source: str) -> list[str | None]:
    """Every touch of self.rfile, mapped to the innermost function holding it.

    Written against the attribute rather than against `self.rfile.read(` on purpose. A detector
    matching the call would be satisfied by a route that bound `f = self.rfile` and read from f,
    which is the same hole in a different shape, and it would miss .readline entirely. Matching the
    attribute catches all three. The innermost enclosing function is what is recorded, so a nested
    helper cannot be credited to the method around it.
    """
    found: list[str | None] = []

    def visit(node: ast.AST, current: str | None) -> None:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            current = node.name
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "rfile"
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
        ):
            found.append(current)
        for child in ast.iter_child_nodes(node):
            visit(child, current)

    visit(ast.parse(source), None)
    return found


@pytest.fixture()
def box(tmp_path: Path, serve_pinecone):
    """The server under test, started by conftest's one helper (spec 012).

    This file used to start serve.py itself on a port derived from the process id. Spec 012 removed
    every such fixture: the port now comes from the OS, so nothing can collide, and the child's
    output comes back through the helper rather than a log file of our own. What the criteria
    assert is unchanged; only the way the server is started has moved.
    """
    data = tmp_path / "data"
    data.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    with serve_pinecone(data=data, args=("--state", str(state))) as served:
        yield served.port, served


@pytest.mark.parametrize("route", ROUTES)
def test_a_negative_length_is_answered_not_awaited(box, route: str) -> None:
    """Criterion 1. Content-Length: -1 gets a prompt 400, and the caller is not left waiting.

    Today min(-1, cap) is -1, which is truthy, and rfile.read(-1) reads to end of file, so the
    handler is held until the caller hangs up.
    """
    port, _served = box
    reply = raw_post(port, route, "-1")
    assert reply.status == 400, f"{route} {reply.outcome} on a negative length, where 400 was expected"
    assert reply.elapsed < 1.0, f"{route} took {reply.elapsed:.1f}s: it is reading to end of file, not answering"


@pytest.mark.parametrize("route", ROUTES)
def test_a_length_that_is_not_a_number_is_answered(box, route: str) -> None:
    """Criterion 2. Content-Length: abc gets a 400 saying so, and nothing reaches the error stream.

    Today int("abc") raises inside do_POST, the connection is dropped with an empty reply, and a
    traceback is printed.
    """
    port, served = box
    reply = raw_post(port, route, "abc")
    assert reply.status == 400, f"{route} {reply.outcome} on a non-numeric length, where 400 was expected"
    assert reply.body.strip(), f"{route} answered 400 with an empty body"
    assert "the request's length is not a number" in reply.body, f"{route} did not say what was wrong: {reply.body!r}"
    assert "Traceback" not in served.said(), f"{route} put a traceback on the error stream"


def test_the_server_is_still_serving_after_a_burst(box) -> None:
    """Criterion 3. A burst of malformed requests is answered, not absorbed, and serving continues.

    Every request in the burst must come back with something. Today the negative ones come back
    with nothing at all, each holding a handler for as long as the caller cares to wait, which is
    the resilience property this card is really about. The 200 at the end is necessary but not
    sufficient: the server is threaded, so it would answer that even while holding parked handlers,
    and a criterion that checked only the 200 would pass today and prove nothing.
    """
    port, _served = box
    unanswered = []
    for _ in range(3):
        for route in ROUTES:
            for header in ("-1", "abc"):
                reply = raw_post(port, route, header, timeout=2.0)
                if reply.status is None:
                    unanswered.append(f"{route} with Content-Length: {header} {reply.outcome}")
    assert not unanswered, f"{len(unanswered)} of the 18 requests were never answered, first three: {unanswered[:3]}"
    assert get_status(port, "/version") == 200, "the server stopped serving after the burst"


def test_only_the_helper_reads_a_request_body(serve) -> None:
    """Criterion 4. One place reads a body, and it stays one place.

    This is what makes the consolidation real rather than aspirational: a route added next year
    that reads the socket directly fails here, whatever else it gets right.

    The source comes from the imported module rather than from a path built to serve.py. Spec 012
    treats building that path as a sign of starting the server, which is the right net to cast
    even though this call only reads it, and asking the module is the honest way round it.
    """
    readers = reads_of_the_request_socket(inspect.getsource(serve))
    assert readers, "no read of the request socket was found at all, so this detector is broken"
    assert HELPER in readers, f"the shared helper {HELPER} does not read the socket, so nothing was consolidated"
    stray = sorted({name for name in readers if name not in ALLOWED_TO_READ_THE_SOCKET}, key=str)
    assert not stray, f"these read the request socket directly instead of through {HELPER}: {stray}"
