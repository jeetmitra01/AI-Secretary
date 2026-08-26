"""
Model bake-off for the extraction stage (ADR-029).

Replays stored emails through a candidate model and scores its output
against the labels already in `secretary.db`. `extraction.py` has carried
a note since ADR-009 that this comparison was "a free experiment in
capability-vs-cost"; this is that experiment.

WHAT THIS MEASURES, EXACTLY. The stored labels are Claude Sonnet 5's own
output, not ground truth. So the headline number is AGREEMENT WITH THE
INCUMBENT, not accuracy. That is still the decision-relevant number —
switching is only safe if the candidate does not lose what the incumbent
caught — but it cannot tell you the incumbent was right, and a
disagreement is not automatically the candidate's error. Read the
disagreement list; never read the percentage alone.

The asymmetry is the whole report:
  MISSED SIGNAL  incumbent said meeting_request / needs_action / question,
                 candidate said automated. The expensive error — the
                 digest silently drops a real item. ADR-016 refused a 92%
                 volume cut over exactly this failure mode.
  FALSE ALARM    the reverse. Costs one noisy digest line. Cheap.

TWO OUTPUT FILES, and the split is a privacy boundary (ADR-030):
  *-summary.json   counts only. No sender, no subject, no message id.
                   Safe to commit, and the only thing git tracks.
  *-rows.json      per-email detail. Real senders and subjects, so it is
                   gitignored with everything else that touches real mail.

Run:
    python eval_extraction.py --limit 25            # cheap smoke run FIRST
    python eval_extraction.py                       # the full labelled set
    python eval_extraction.py --model claude-sonnet-5   # incumbent vs itself
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

BASE = Path(__file__).parent
load_dotenv(BASE / ".env")                    # before Anthropic(), as ever

from anthropic import Anthropic                                    # noqa: E402
import store                                                       # noqa: E402
from console import use_utf8                                       # noqa: E402
from extraction import (MODEL, PROMPT, Extraction,                 # noqa: E402
                        extract_email)
from models import Email                                           # noqa: E402

use_utf8()

EVALS_DIR = BASE / "evals"
DEFAULT_CANDIDATE = "claude-haiku-4-5"

# The intents whose loss is a product failure rather than a cosmetic one.
# "fyi" is deliberately NOT here: mislabelling an fyi as automated costs a
# digest line, not a meeting.
SIGNAL = {"meeting_request", "needs_action", "question"}

# How a failed extraction appears in the confusion matrix. A real intent
# can never collide with it, because the Extraction enum has no such value.
FAILED = "FAILED"

# USD per million tokens (input, output), list price, as of 2026-08-26.
#
# Sonnet 5 is on INTRODUCTORY pricing of $2/$10 through 2026-08-31; the
# $3/$15 below is what it reverts to. So the incumbent's true cost is
# about to rise ~50% on its own, independent of anything decided here.
# Hardcoded because this is a one-shot report, not a billing system.
PRICES = {
    "claude-sonnet-5":  (3.00, 15.00),
    "claude-haiku-4-5": (1.00,  5.00),
}

# Rough public rule of thumb, NOT a tokenizer. Only ever used to compare
# two models on one axis; for real numbers use client.messages.count_tokens.
CHARS_PER_TOKEN = 4
EST_OUTPUT_TOKENS = 200          # one filled-in Extraction, measured shape


def load_labelled(conn, limit: int | None) -> list[tuple[Email, str]]:
    """Every stored email with a body to replay AND a successful
    extraction to score against, newest first (so `--limit` samples
    recent mail rather than arbitrary mail).

    Two exclusions, both deliberate. Failed extractions: `intent` is NULL
    there, so there is nothing to agree or disagree with. Orphan
    extractions (the pre-ADR-019 rows backfill.py imported): the JOIN
    drops them, because their bodies were never stored and so cannot be
    replayed at all.
    """
    sql = """
        SELECT e.id, e.thread_id, e.sender, e.recipients, e.subject,
               e.body_text, e.received_at, e.source, x.intent
        FROM emails e
        JOIN extractions x ON x.email_id = e.id
        WHERE x.error IS NULL AND length(e.body_text) > 0
        ORDER BY e.received_at DESC
    """
    if limit:
        sql += f" LIMIT {int(limit)}"

    rows = []
    for r in conn.execute(sql):
        received = (datetime.fromisoformat(r["received_at"])
                    if r["received_at"] else None)
        rows.append((Email(id=r["id"], thread_id=r["thread_id"],
                           sender=r["sender"],
                           recipients=json.loads(r["recipients"]),
                           subject=r["subject"], body_text=r["body_text"],
                           received_at=received, source=r["source"]),
                     r["intent"]))
    return rows


def replay(client, rows, model: str, workers: int) -> list[dict]:
    """Run the candidate over every row.

    Calls the pipeline's own `extract_email` rather than a local copy: a
    bake-off against a reimplementation measures the reimplementation.
    """
    def one(item):
        email, incumbent = item
        result = extract_email(client, email, model=model)
        ok = isinstance(result, Extraction)
        return {"id": email.id, "sender": email.sender,
                "subject": email.subject[:70],
                "incumbent": incumbent,
                "candidate": result.intent if ok else None,
                "error": None if ok else result.error}

    scored, done = [], 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for row in pool.map(one, rows):
            scored.append(row)
            done += 1
            if done % 25 == 0 or done == len(rows):
                print(f"  {done}/{len(rows)}", end="\r", file=sys.stderr)
    print(file=sys.stderr)
    return scored


def build_summary(scored: list[dict], model: str, elapsed: float,
                  workers: int, rows_file: str) -> dict:
    """The committable half: counts and nothing else (ADR-030).

    THE DIRECTION MATTERS. This is assembled field by field from
    aggregates. It is NOT a copy of a row with the identifying keys
    deleted, and it must never become one — a strip-the-bad-keys function
    leaks the next key somebody adds to `replay()`, silently and by
    default. Counting cannot leak a field it was never told about.

    A test asserts that nothing in here matches a sender or a subject.
    """
    signal_rows = [s for s in scored if s["incumbent"] in SIGNAL]
    missed = [s for s in scored
              if s["incumbent"] in SIGNAL and s["candidate"] == "automated"]

    # incumbent -> candidate -> count. Aggregate, so it carries far more
    # information than the four headline numbers and still names nobody.
    matrix: dict[str, Counter] = {}
    for s in scored:
        matrix.setdefault(s["incumbent"], Counter())[
            s["candidate"] or FAILED] += 1

    return {
        "model": model,
        "incumbent_model": MODEL,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "workers": workers,
        "elapsed_s": round(elapsed, 1),
        "scored": len(scored),
        "agreed": sum(1 for s in scored
                      if s["candidate"] == s["incumbent"]),
        "failures": sum(1 for s in scored if s["candidate"] is None),
        "signal_rows": len(signal_rows),
        "missed_signal": len(missed),
        "false_alarms": sum(1 for s in scored
                            if s["incumbent"] == "automated"
                            and s["candidate"] in SIGNAL),
        "confusion": {k: dict(sorted(v.items()))
                      for k, v in sorted(matrix.items())},
        "detail_file": rows_file,      # a filename, not its contents
    }


def estimate_input_tokens(rows) -> float:
    """Mean prompt size, built from the REAL prompt the pipeline sends —
    the schema dump and the 1500-char body cap included."""
    total = sum(len(PROMPT.format(email_block=e.brief(body_chars=1500),
                                  email_id=e.id,
                                  schema=json.dumps(
                                      Extraction.model_json_schema(), indent=1)))
                for e, _ in rows)
    return total / len(rows) / CHARS_PER_TOKEN


def report(scored: list[dict], rows, summary: dict) -> None:
    """Console output for the operator. This one DOES print senders and
    subjects — reading the misses is how ADR-029 was decided — and that
    is fine, because the console is not a committed artefact. Only
    `build_summary` feeds a tracked file.
    """
    total = summary["scored"]
    missed = [s for s in scored
              if s["incumbent"] in SIGNAL and s["candidate"] == "automated"]
    failures = [s for s in scored if s["candidate"] is None]
    other = (total - summary["agreed"] - summary["failures"]
             - summary["missed_signal"] - summary["false_alarms"])

    print(f"\n=== {summary['model']} vs the stored labels ===")
    print(f"  scored              {total}")
    print(f"  agreed              {summary['agreed']} "
          f"({100 * summary['agreed'] / total:.1f}%)")
    print(f"  extraction failures {summary['failures']}"
          f"   (tripwire 1/2 fired: id mismatch or bad schema)")
    print(f"  wall clock          {summary['elapsed_s']:.0f}s at "
          f"{summary['workers']} workers")

    n_signal = summary["signal_rows"]
    kept = n_signal - summary["missed_signal"]
    pct = f" ({100 * kept / n_signal:.0f}%)" if n_signal else ""
    print("\n  -- the asymmetry --")
    print(f"  signal rows         {n_signal}"
          f"   (meeting_request / needs_action / question)")
    print(f"  MISSED SIGNAL       {summary['missed_signal']}"
          f"   -> kept {kept}/{n_signal}{pct}")
    print(f"  false alarms        {summary['false_alarms']}   (cheap)")
    print(f"  other disagreement  {other}   (fyi/automated shuffling)")

    if missed:
        print("\n  MISSED — read every one of these before switching:")
        for s in missed:
            print(f"    {s['incumbent']:<15} -> automated   "
                  f"{s['sender'][:38]} | {s['subject']}")
    if failures:
        print("\n  FAILED:")
        for s in failures[:10]:
            print(f"    {s['id']}: {str(s['error'])[:100]}")

    in_tok = estimate_input_tokens(rows)
    print("\n  -- cost per 100 emails, list price, ~4 chars/token --")
    for name, (p_in, p_out) in PRICES.items():
        per100 = 100 * (in_tok * p_in + EST_OUTPUT_TOKENS * p_out) / 1e6
        tag = "  <- candidate" if name == summary["model"] else ""
        note = "  (rises to this on 2026-08-31)" if name == MODEL else ""
        print(f"    {name:<18} ${per100:.3f}{tag}{note}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Score a candidate extraction model against the store.")
    ap.add_argument("--model", default=DEFAULT_CANDIDATE,
                    help=f"candidate model (default {DEFAULT_CANDIDATE}); "
                         f"pass {MODEL} to measure the incumbent's own "
                         f"run-to-run variance")
    ap.add_argument("--limit", type=int, default=None,
                    help="score only the newest N emails; do a small run "
                         "first, the full set costs real money")
    ap.add_argument("--workers", type=int, default=4,
                    help="concurrent calls (ADR-028's number)")
    args = ap.parse_args()

    conn = store.connect()
    try:
        rows = load_labelled(conn, args.limit)
    finally:
        conn.close()

    if not rows:
        sys.exit("no labelled emails with stored bodies — run the digest first")

    print(f"replaying {len(rows)} labelled emails through {args.model}")
    started = time.monotonic()
    scored = replay(Anthropic(), rows, args.model, args.workers)
    elapsed = time.monotonic() - started

    EVALS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M")
    rows_path = EVALS_DIR / f"{stamp}-{args.model}-rows.json"
    summary_path = EVALS_DIR / f"{stamp}-{args.model}-summary.json"

    summary = build_summary(scored, args.model, elapsed, args.workers,
                            rows_path.name)
    report(scored, rows, summary)

    summary_path.write_text(json.dumps(summary, indent=1), encoding="utf-8")
    rows_path.write_text(json.dumps(scored, indent=1, ensure_ascii=False),
                         encoding="utf-8")
    print(f"\nsummary (committable): {summary_path}")
    print(f"detail   (gitignored):  {rows_path}")
