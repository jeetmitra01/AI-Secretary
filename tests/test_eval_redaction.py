"""
Tests for the eval privacy boundary (ADR-030).

Same split as tests/test_proposals.py and tests/test_console.py:

  1. INVARIANT — the committable summary carries no sender, subject, or
     message id, and its key set is fixed. This is the test that actually
     enforces ADR-030. `build_summary` is safe today because it COUNTS;
     the failure mode it guards against is a future edit that "simplifies"
     it into a copy-the-row-and-delete-the-bad-keys function, which leaks
     the next key somebody adds to replay() silently and by default.

  2. BEHAVIOUR — the counts and the confusion matrix are right, and
     .gitignore really does ignore the detail file while letting the
     summary through. A correct build_summary paired with a gitignore
     that ships the rows file would still be a leak.

Nothing here touches Anthropic or the network.

Run:  python -m pytest tests -q
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import eval_extraction as E                              # noqa: E402


# --- fixtures --------------------------------------------------------------

# Distinctive, unmistakable tokens. If any of these reaches the summary,
# the substring assertions below cannot miss it.
SENDER = "Jane Doe <jane.doe@leasing-office.example>"
SUBJECT = "1710 Hanford Dr 3, San Diego, CA 92111"
MSG_ID = "1a03a140dc79a517"


def rows() -> list[dict]:
    """A shaped sample: one agreement, one MISSED SIGNAL, one false
    alarm, one extraction failure."""
    return [
        {"id": MSG_ID, "sender": SENDER, "subject": SUBJECT,
         "incumbent": "meeting_request", "candidate": "meeting_request",
         "error": None},
        {"id": "b2", "sender": SENDER, "subject": SUBJECT,
         "incumbent": "meeting_request", "candidate": "automated",
         "error": None},
        {"id": "c3", "sender": SENDER, "subject": SUBJECT,
         "incumbent": "automated", "candidate": "needs_action",
         "error": None},
        {"id": "d4", "sender": SENDER, "subject": SUBJECT,
         "incumbent": "automated", "candidate": None,
         "error": "schema: 1 validation error"},
    ]


def summary_of(sample) -> dict:
    return E.build_summary(sample, model="claude-haiku-4-5", elapsed=1.0,
                           workers=4, rows_file="2026-01-01-x-rows.json")


# --- 1. INVARIANT ----------------------------------------------------------

def test_summary_leaks_no_sender_subject_or_id():
    """The point of the whole ADR. Serialize the summary and search it for
    every identifying string that went in."""
    blob = json.dumps(summary_of(rows()))

    for secret in (SENDER, SUBJECT, MSG_ID, "jane.doe", "Hanford",
                   "leasing-office.example"):
        assert secret not in blob, (
            f"{secret!r} reached the committable summary. build_summary "
            f"must assemble counts, never copy a row and delete keys.")


def test_summary_key_set_is_fixed():
    """A new key on a row must not become a new key on the summary.

    This is the mutation the substring test alone would not catch: add
    `"body_preview"` to replay()'s dict, and a strip-based summary would
    start shipping it while every existing assertion still passed.
    """
    expected = {"model", "incumbent_model", "generated_at", "workers",
                "elapsed_s", "scored", "agreed", "failures", "signal_rows",
                "missed_signal", "false_alarms", "confusion", "detail_file"}
    assert set(summary_of(rows())) == expected


def test_a_new_row_field_cannot_reach_the_summary():
    """The same invariant, driven from the other end: bolt an unexpected
    field onto every row and prove the summary is byte-identical."""
    plain = summary_of(rows())

    tainted = rows()
    for r in tainted:
        r["body_preview"] = "CONFIDENTIAL-BODY-TEXT"
        r["recipients"] = ["someone@example.com"]
    poisoned = summary_of(tainted)

    plain.pop("generated_at"), poisoned.pop("generated_at")   # a clock, not data
    assert plain == poisoned
    assert "CONFIDENTIAL" not in json.dumps(poisoned)


# --- 2. BEHAVIOUR ----------------------------------------------------------

def test_counts_and_asymmetry():
    s = summary_of(rows())

    assert s["scored"] == 4
    assert s["agreed"] == 1
    assert s["failures"] == 1
    assert s["signal_rows"] == 2          # the two meeting_request rows
    assert s["missed_signal"] == 1        # meeting_request -> automated
    assert s["false_alarms"] == 1         # automated -> needs_action


def test_confusion_matrix_records_failures_without_colliding():
    """A failed extraction has candidate None. It must appear in the
    matrix as FAILED rather than being dropped (ADR-009's tripwire 3:
    failures are rows, never omissions) — and FAILED must not be able to
    collide with a real intent."""
    s = summary_of(rows())

    assert s["confusion"]["automated"][E.FAILED] == 1
    assert s["confusion"]["meeting_request"] == {"meeting_request": 1,
                                                 "automated": 1}
    assert E.FAILED not in set(E.Extraction.model_fields["intent"]
                               .annotation.__args__)


def test_empty_input_does_not_divide_by_zero():
    s = summary_of([])
    assert s["scored"] == 0 and s["signal_rows"] == 0 and s["confusion"] == {}


# --- 3. the gitignore half of the boundary ---------------------------------

@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
@pytest.mark.parametrize("name, ignored", [
    ("2026-08-26-0858-claude-haiku-4-5-rows.json", True),
    ("2026-08-26-0858-claude-haiku-4-5-summary.json", False),
    ("scratch.json", True),          # deny-by-default: anything new is out
    ("rows.txt", True),
])
def test_gitignore_ships_only_summaries(name: str, ignored: bool):
    """A correct build_summary and a leaky .gitignore is still a leak.
    `git check-ignore -q` exits 0 when the path is ignored, 1 when not."""
    result = subprocess.run(
        ["git", "check-ignore", "-q", f"evals/{name}"],
        cwd=REPO, capture_output=True, timeout=30)

    assert (result.returncode == 0) is ignored, (
        f"evals/{name}: expected ignored={ignored}, "
        f"git check-ignore returned {result.returncode}")
