# ADR-021: Console encoding is fixed in code, once, and enforced by a test

Date: 2026-08-17
Status: accepted

## Context

Every entrypoint prints text it did not write: model output, email
subjects, sender names, calendar titles. A Windows console defaults to
cp1252, so any character outside that set raises UnicodeEncodeError at
print time.

The failure mode is the expensive part. `python secretary_graph.py`
completed a full agent run — API call paid for, tools executed, answer
produced — and then died printing it, because the model chose 🔴 as a
priority marker. The same shape had already been hit in `run_digest.py`,
where the crash landed after the digest file was written and before the
toast fired, killing the only channel that reaches the user.

The fix was three lines, and it had been copied into `run_digest.py`,
`server.py`, and `backfill.py`. The four entrypoints that printed
untrusted text without it were exactly the ones that crashed.

## Decision

One `console.use_utf8()`, called first thing by every entrypoint.
`tests/test_console.py` fails if a module with a `__main__` block does
not call it.

## Why

- The rule was already known and still broken three times over. A
  convention that depends on remembering is not a convention; the test
  is what makes it one.
- Correctness must not depend on how a process was launched. The
  scheduled task, a fresh terminal, and a double-clicked script are
  three different environments, and the one that matters most is the one
  nobody configured.
- `errors="replace"` degrades instead of dying: a lost glyph beats a
  lost report.

## Rejected

- **`PYTHONUTF8=1` / `PYTHONIOENCODING` in the environment.** Fixes
  every entrypoint at once, and is invisible. It lives in the launcher
  rather than the repo, so a clone, a new terminal profile, or an edited
  scheduled task silently loses it — and the symptom returns as a crash
  in unrelated code.
- **Encode at each print site** (`errors="replace"` per call, or ASCII
  fallbacks). Every future print is a new chance to forget, and it
  mangles output that the terminal could have shown correctly.
- **Strip non-ASCII from model output.** Treats the symptom, damages the
  data, and would also have to strip real senders' names.

## Consequences

- A new entrypoint fails its test until it calls `use_utf8()`. That is
  the intent; the failure message says what to add.
- `run_digest.py` calls it at IMPORT time, not in `__main__`, because
  `server.py` imports the module and inherits the fix. A module that
  reconfigures streams on import is normally rude; here it is the
  reason the service never hits this.
- The test suite asserts its own hostile environment (test 1 requires
  the unguarded child to FAIL). If a future Python makes utf-8 the
  console default, that test breaks loudly rather than going quiet.
