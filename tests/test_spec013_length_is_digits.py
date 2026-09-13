"""Spec 013, criterion 2 carried to its edge: a length Python will parse but HTTP will not.

Everything here is synthetic. No real client, exercise or estate data.

Not an acceptance test. The four acceptance criteria are in test_spec013_body_length.py and that
file is closed. This one was written during the code-review pass, after the fix had landed, and it
covers a gap the criterion describes but its own test does not reach.

Criterion 2 says a length that is not a number gets a 400 saying so, and its test proves that for
"abc". int() is looser than the criterion: it accepts an underscore separator and a leading plus,
so Content-Length: 1_0 was read as ten bytes and believed. Content-Length is 1*DIGIT and nothing
else. This repository already treats that exact leniency as a defect, at serve.py:1096, where a
form field is parsed with .replace("_", "x") and a comment saying int() would otherwise accept
1_000. The helper now carries the same guard.

A negative length is deliberately not in here. Spec 013 clamps it to zero rather than answering
400, so "-1" stays parseable and criterion 1 owns it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# The acceptance file's harness is reused rather than copied: hand-rolled server fixtures with
# their own port bands are how this suite has collided with itself before. Imported flat, not as
# tests.<name>, because there is no tests/__init__.py and mypy then sees one file under two names.
from test_spec013_body_length import ANSWERED, ROUTES, raw_post

# Lengths int() accepts and HTTP does not. Each would be believed as a byte count if the guard
# were removed, so each is a body read on a claim the caller never validly made.
NOT_A_NUMBER_TO_HTTP = ["1_0", "+5"]


@pytest.fixture()
def box(tmp_path: Path, serve_pinecone):
    """Started by conftest's one helper (spec 012), so this file picks no port either."""
    data = tmp_path / "data"
    data.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    with serve_pinecone(data=data, args=("--state", str(state))) as served:
        yield served.port, served


@pytest.mark.parametrize("length_header", NOT_A_NUMBER_TO_HTTP)
@pytest.mark.parametrize("route", ROUTES)
def test_a_length_python_accepts_but_http_does_not_is_refused(box, route: str, length_header: str) -> None:
    """A length of 1_0 or +5 is refused with the criterion's own wording, promptly and quietly."""
    port, served = box
    reply = raw_post(port, route, length_header)
    assert reply.outcome == ANSWERED, f"{route} {reply.outcome} on Content-Length: {length_header}"
    assert reply.status == 400, f"{route} answered {reply.status} on Content-Length: {length_header}, not 400"
    assert (
        "length is not a number" in reply.body
    ), f"{route} answered 400 on Content-Length: {length_header} for some other reason: {reply.body!r}"
    assert reply.elapsed < 1.0, f"{route} took {reply.elapsed:.1f}s to refuse Content-Length: {length_header}"
    assert "Traceback" not in served.said(), f"{route} put a traceback on the error stream"


def test_a_plain_digit_length_still_works(box) -> None:
    """The guard refuses the malformed without refusing the ordinary: a real length still reads.

    Without this, a guard that rejected every length would pass the test above and break the
    server, which is the failure mode a negative test cannot see on its own.
    """
    port, _served = box
    reply = raw_post(port, "/api/maps/choose", "9", payload=b"id=nosuch")
    assert reply.status == 400, f"a well-formed length gave {reply.status}, so the body was not read"
    assert "length is not a number" not in reply.body, "a well-formed length was refused as malformed"
