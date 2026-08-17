"""
One job: make stdout/stderr accept the characters our own output contains.

The failure this prevents, in full. A Windows console defaults to cp1252.
Any character outside that set — an em-dash, the digest's ⚠, an emoji the
model chose for a priority marker, an accent in a sender's name — raises
UnicodeEncodeError at PRINT time. So the work all succeeded: the API call
was paid for, the tools ran, the digest file was written utf-8. Then the
process died reporting it. That is the worst possible place to fail,
because the failure is in the channel that tells you anything at all.

The rule this encodes: text we print comes from a model or from a third
party, so we never get to assume it is ASCII. The console is ours to fix.

Why a shared module and not three lines per entrypoint: it was already
copied into run_digest.py, server.py, and backfill.py, and the four
entrypoints that printed untrusted text WITHOUT it are exactly the ones
that crashed. A rule enforced by remembering is not enforced.
tests/test_console.py checks every entrypoint calls this.

Why in code rather than PYTHONIOENCODING/PYTHONUTF8 in the environment:
the scheduled task, a fresh terminal, and a double-clicked script are
three different environments, and the one that matters most is the one
nobody configured. Correctness should not depend on how a process was
launched.
"""

from __future__ import annotations

import sys


def use_utf8() -> None:
    """Reconfigure stdout and stderr to utf-8. Idempotent; never raises.

    Call it first thing in any entrypoint that prints.

    `errors="replace"` is the belt-and-braces half: if a stream cannot be
    switched, a stray character then prints as a replacement mark instead
    of killing the process. Losing one glyph beats losing the report.
    """
    for stream in (sys.stdout, sys.stderr):
        # None under pythonw.exe (no console at all), and pytest's capture
        # objects have no reconfigure either. Both mean "nothing to fix".
        if stream is None or not hasattr(stream, "reconfigure"):
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            # A detached or already-wrapped stream. Printing is still
            # better than refusing to start.
            pass
