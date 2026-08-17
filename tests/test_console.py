"""
Regression tests for the cp1252 crash (console.py).

The bug: `python secretary_graph.py` completed a full agent run, then died
with UnicodeEncodeError while PRINTING the answer, because the model used
🔴 as a priority marker and a Windows console is cp1252 by default.

Two kinds of test here, and the split is the point:

  1. BEHAVIOUR — reproduce the crash in a child process with a cp1252
     stdout, then prove the guard fixes it. Test 1 deliberately asserts
     the FAILURE: without it, tests 2 and 3 could both pass against a
     no-op guard and we would learn nothing.

  2. INVARIANT — every entrypoint calls the guard. This is the test that
     actually stops a recurrence. The behaviour tests prove console.py
     works; only this one catches the eighth entrypoint, written months
     from now, that forgets to call it.

Run:  python -m pytest tests -q
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# The exact character from the traceback: U+1F534 LARGE RED CIRCLE. Not
# encodable in cp1252, so it is a faithful stand-in for anything a model
# or a sender might send us.
EMOJI = "\U0001f534"


def run_child(code: str) -> subprocess.CompletedProcess:
    """Run `code` in a child whose stdout really is cp1252.

    PYTHONIOENCODING with no `:errors` suffix means strict, which is what
    a real console does. PYTHONUTF8=0 is not redundant — UTF-8 mode would
    silently rescue the child and make these tests lie.

    text=False: we want the child's RAW bytes. Decoding here with the
    parent's encoding would move the failure into the test process.
    """
    env = {**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"}
    return subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env,
                          capture_output=True, text=False, timeout=60)


# --- 1. the crash, reproduced ----------------------------------------------

def test_cp1252_stdout_fails_without_the_guard():
    """The failing case. If this test ever passes-by-succeeding, the
    others prove nothing, because the environment stopped being hostile."""
    result = run_child(f"print({EMOJI!r})")

    assert result.returncode != 0, (
        "a cp1252 stdout accepted an emoji — the hostile environment these "
        "tests depend on is no longer hostile, so tests 2 and 3 are vacuous")
    assert b"UnicodeEncodeError" in result.stderr


# --- 2. the guard fixes it -------------------------------------------------

def test_guard_makes_non_ascii_printable():
    result = run_child(f"from console import use_utf8;"
                       f"use_utf8();"
                       f"print({EMOJI!r})")

    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert EMOJI.encode("utf-8") in result.stdout


def test_guard_is_idempotent():
    """Called twice because run_digest.py calls it at import time and
    server.py calls it again in __main__ — a real, reachable sequence."""
    result = run_child(f"from console import use_utf8;"
                       f"use_utf8(); use_utf8();"
                       f"print({EMOJI!r})")

    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")


def test_guard_survives_a_stream_it_cannot_reconfigure():
    """pytest's capture objects and pythonw.exe's None streams both hit
    this path. The guard must skip them, not raise."""
    result = run_child(
        "import io, sys;"
        "sys.stdout = io.StringIO();"       # no .reconfigure
        "sys.stderr = None;"                # pythonw.exe
        "from console import use_utf8;"
        "use_utf8();"
        "sys.__stdout__.write('survived')")

    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert b"survived" in result.stdout


def test_digest_characters_specifically():
    """The characters our own composition step emits, as opposed to
    anything a model invents: the ⚠ warning marker and an em-dash."""
    result = run_child("from console import use_utf8;"
                       "use_utf8();"
                       "print('⚠ 2 emails failed — see runs/')")

    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")


# --- 3. the invariant: no entrypoint may forget ----------------------------

def entrypoints() -> list[Path]:
    """Every module at the repo root with an `if __name__ == "__main__":`
    block. That block is what makes a file something a human runs, and
    running it is when the console encoding starts to matter."""
    found = []
    for path in sorted(REPO.glob("*.py")):
        if path.name in {"console.py", "test_script.py"}:
            continue        # console.py IS the guard; test_script.py is scratch
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if (isinstance(node, ast.If)
                    and ast.unparse(node.test) == "__name__ == '__main__'"):
                found.append(path)
                break
    return found


def test_entrypoints_were_found():
    """A guard on the guard: if the AST walk above silently matched
    nothing, the parametrized test below would report all-green while
    checking zero files."""
    names = {p.name for p in entrypoints()}
    assert {"secretary_graph.py", "connectors.py", "run_digest.py"} <= names, (
        f"entrypoint detection looks broken; found {sorted(names)}")


@pytest.mark.parametrize("path", entrypoints(), ids=lambda p: p.name)
def test_every_entrypoint_calls_use_utf8(path: Path):
    """The recurrence test. secretary_graph.py failed exactly because it
    was an entrypoint that printed model output without this call, while
    run_digest.py and server.py had a private copy of the fix."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))

    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             and getattr(n.func, "id", None) == "use_utf8"]

    assert calls, (
        f"{path.name} is an entrypoint but never calls use_utf8(). Any "
        f"non-ASCII character it prints — a model's emoji, an accented "
        f"sender name, the digest's warning marker — will crash it on a "
        f"cp1252 console. Add `from console import use_utf8` and call it "
        f"before the first print.")
