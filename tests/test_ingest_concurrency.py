"""
Tests for concurrent extraction (ADR-028).

Same split as tests/test_proposals.py:

  1. INVARIANT — results come back in INPUT order, not completion order.
     This is the test that actually enforces ADR-028's one risk. The
     sequential loop got ordering for free; the pool does not, and a
     future edit to as_completed() for "faster first output" would
     reshuffle the digest silently. Nothing else in the system would
     notice: the digest would still be correct, just no longer
     newest-first (ADR-011).

  2. BEHAVIOUR — the work really is concurrent, and the
     extraction/failure split still partitions correctly.

Nothing here touches Anthropic: extract_email is monkeypatched, which
is exactly why extract_all takes the client as an argument.

Run:  python -m pytest tests -q
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import run_digest                                       # noqa: E402
from extraction import Extraction, ExtractionFailure    # noqa: E402
from models import Email                                # noqa: E402


# --- helpers ---------------------------------------------------------------

def make_emails(n: int) -> list[Email]:
    """n emails, newest first, as fetch_since returns them (ADR-011)."""
    return [
        Email(id=f"m{i:02d}", thread_id=f"t{i:02d}", sender="a@b.com",
              subject=f"subject {i}", body_text="body",
              received_at=datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc),
              source="gmail")
        for i in range(n)
    ]


def ok(email: Email) -> Extraction:
    return Extraction(id=email.id, intent="fyi", summary="s", priority="low")


# --- 1. INVARIANT ----------------------------------------------------------

def test_results_keep_input_order_when_workers_finish_out_of_order(monkeypatch):
    """The whole point. Earlier emails are made to take LONGER, so
    completion order is close to reversed. Input order must survive."""
    emails = make_emails(8)

    def slow_for_early_items(client, email):
        # m00 sleeps longest, m07 shortest -> completion order inverts
        time.sleep((8 - int(email.id[1:])) * 0.02)
        return ok(email)

    monkeypatch.setattr(run_digest, "extract_email", slow_for_early_items)
    monkeypatch.setattr(run_digest, "EXTRACT_WORKERS", 4)

    extractions, failures = run_digest.extract_all(client=None, emails=emails)

    assert failures == []
    assert [e.id for e in extractions] == [e.id for e in emails]


def test_failures_also_keep_input_order(monkeypatch):
    """The split walks one ordered stream, so both lists must stay
    ordered — not just the successful one."""
    emails = make_emails(6)

    def every_other_fails(client, email):
        i = int(email.id[1:])
        time.sleep((6 - i) * 0.02)
        if i % 2:
            return ExtractionFailure(id=email.id, error="boom")
        return ok(email)

    monkeypatch.setattr(run_digest, "extract_email", every_other_fails)
    monkeypatch.setattr(run_digest, "EXTRACT_WORKERS", 4)

    extractions, failures = run_digest.extract_all(client=None, emails=emails)

    assert [e.id for e in extractions] == ["m00", "m02", "m04"]
    assert [f.id for f in failures] == ["m01", "m03", "m05"]
    assert len(extractions) + len(failures) == len(emails)


# --- 2. BEHAVIOUR ----------------------------------------------------------

def test_extraction_actually_runs_concurrently(monkeypatch):
    """Guards against a refactor that keeps the function but loses the
    pool. 8 emails x 0.1s is 0.8s sequential, ~0.2s across 4 workers."""
    emails = make_emails(8)

    def takes_a_tenth_of_a_second(client, email):
        time.sleep(0.1)
        return ok(email)

    monkeypatch.setattr(run_digest, "extract_email", takes_a_tenth_of_a_second)
    monkeypatch.setattr(run_digest, "EXTRACT_WORKERS", 4)

    started = time.monotonic()
    extractions, failures = run_digest.extract_all(client=None, emails=emails)
    elapsed = time.monotonic() - started

    assert len(extractions) == 8 and failures == []
    assert elapsed < 0.5, f"took {elapsed:.2f}s — is the pool still there?"


def test_empty_input_is_not_an_error(monkeypatch):
    """A run with nothing new is the common case, and ThreadPoolExecutor
    on an empty iterable must not be a special path."""
    monkeypatch.setattr(run_digest, "EXTRACT_WORKERS", 4)
    assert run_digest.extract_all(client=None, emails=[]) == ([], [])
