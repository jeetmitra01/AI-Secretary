"""
Phase 1, stage 3 + orchestrator.

Delivery is local-first: full digest -> dated markdown file in runs/,
Windows toast shows code-computed counts and opens the file on click.
Delivery is deliberately just a function — swapping it later for
send-to-self email is a new ADR, not a refactor.

Run manually:      python run_digest.py
Schedule (Windows, twice daily): see SCHEDULING.md
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

BASE = Path(__file__).parent
load_dotenv(BASE / ".env")                      # before Anthropic() — the
                                                # import-order lesson, encoded
from anthropic import Anthropic                 # noqa: E402
import store                                    # noqa: E402
from auth import ReauthorizationRequired                    # noqa: E402
from connectors import CONNECTORS                           # noqa: E402
from extraction import Extraction, ExtractionFailure, extract_email  # noqa: E402
from composition import Coverage, compose_digest, toast_summary      # noqa: E402

# At IMPORT time, not in __main__, and that placement is deliberate: the
# digest carries "⚠" and em-dashes, and a cp1252 console would raise
# UnicodeEncodeError AFTER the digest file is written but BEFORE the toast
# fires — killing the one channel that reaches you. server.py imports this
# module, so it inherits the fix. Reasoning: console.py.
from console import use_utf8                      # noqa: E402

use_utf8()

EMAIL_PROVIDER = "gmail"
STATE_FILE = BASE / "state.json"     # watermark + seen ids (ADR-006, persisted)
RUNS_DIR = BASE / "runs"
OVERLAP = timedelta(minutes=5)
FIRST_RUN_LOOKBACK = timedelta(hours=12)
MAX_FETCH = 100                      # bodies per run; count_since tells us
                                     # when the window overflowed it (ADR-012)
MAX_SEEN_IDS = 5000                  # keep the newest N; older ones are
                                     # outside any future fetch window anyway


# --- state -----------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_run_utc": None, "seen_ids": []}


def save_state(state: dict) -> None:
    state["seen_ids"] = state["seen_ids"][-MAX_SEEN_IDS:]
    STATE_FILE.write_text(json.dumps(state, indent=1))


# --- delivery --------------------------------------------------------------

def toast(title: str, msg: str, launch: str | None = None) -> None:
    """Best-effort: a missing lib or a non-Windows host must never kill a
    run. Also the only channel a scheduled run has to reach you at all."""
    try:
        from winotify import Notification
        note = Notification(app_id="Secretary", title=title, msg=msg,
                            **({"launch": launch} if launch else {}))
        note.show()
    except Exception as e:
        print(f"[toast unavailable: {e}]", file=sys.stderr)


def deliver(result: RunResult) -> None:
    """Notify + show. The file write moved into the pipeline itself when
    ADR-019 demoted runs/ from "the data" to "the archive": the record
    must exist whether or not anyone is being notified, and a scheduled
    run and an API-triggered one must archive identically. What is left
    here is what ADR-014 was actually about — the channel that reaches
    the user."""
    print(result.digest)                           # console always works

    title, line = toast_summary(result.extractions, result.coverage)
    digest_file = result.run_dir / "digest.md"
    toast(title, line, launch=str(digest_file.resolve()))  # click -> digest


# --- orchestrator ----------------------------------------------------------

@dataclass(frozen=True)
class RunResult:
    """What one ingestion produced. Exists so the pipeline has a return
    value: ADR-020's service needs to trigger the same code path the
    scheduler runs, and a function whose only output is a toast cannot
    be called by anything else."""
    run_id: str
    run_dir: Path
    digest: str
    extractions: list[Extraction]
    failures: list[ExtractionFailure]
    coverage: Coverage


def ingest(client: Anthropic | None = None) -> RunResult:
    """Fetch -> extract -> compose -> persist. The whole pipeline, minus
    delivery.

    Raises ReauthorizationRequired with the watermark untouched, so the
    next run re-covers the same window (ADR-006). Callers decide how to
    report that: the CLI toasts and exits nonzero, the service returns
    an error status.
    """
    client = client or Anthropic()
    # No allow_interactive_auth: this entrypoint is the scheduled one, so
    # a dead token must raise, never try to open a browser.
    connector = CONNECTORS[EMAIL_PROVIDER]()
    state = load_state()

    now = datetime.now(timezone.utc)
    since = (datetime.fromisoformat(state["last_run_utc"]) - OVERLAP
             if state["last_run_utc"] else now - FIRST_RUN_LOOKBACK)
    seen = set(state["seen_ids"])

    # State is deliberately NOT saved if this raises: the watermark stays
    # put, so once you re-consent the next run re-covers this whole
    # window (ADR-006).
    #
    # Count BEFORE fetching, and pass the SAME skip_ids to both so the
    # two numbers describe one population. Counting after would let
    # mail arriving between the calls look like a shortfall, which
    # would pin the watermark below on a backlog that isn't there.
    pending = connector.count_since(since, skip_ids=seen)
    emails = connector.fetch_since(since, max_results=MAX_FETCH,
                                   skip_ids=seen)

    # No post-fetch dedupe pass: skip_ids applied it during listing, which
    # is what lets a backlog drain instead of re-serving the same newest
    # MAX_FETCH every run (ADR-015).
    coverage = Coverage(total_matching=pending, fetched=len(emails))
    print(f"unread in window {pending}, read this run {len(emails)}"
          + (f", NOT covered {coverage.missed}" if coverage.truncated else ""))

    extractions: list[Extraction] = []
    failures: list[ExtractionFailure] = []
    for email in emails:
        result = extract_email(client, email)
        (extractions if isinstance(result, Extraction) else failures).append(result)
        print(f"  {email.id}: "
              f"{result.intent if isinstance(result, Extraction) else 'FAILED'}")

    digest = compose_digest(client, extractions, failures, coverage)

    # every run archived: extraction JSONs are your eval dataset
    run_id = datetime.now().strftime("%Y-%m-%d-%H%M")
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "extractions.json").write_text(json.dumps(
        {"coverage": {"total_matching": coverage.total_matching,
                      "fetched": coverage.fetched,
                      "missed": coverage.missed},
         "ok": [e.model_dump() for e in extractions],
         "failed": [f.model_dump() for f in failures]}, indent=1))
    (run_dir / "digest.md").write_text(digest, encoding="utf-8")

    # ADR-019: the store, and BEFORE the watermark moves. If a write here
    # fails, the exception leaves last_run_utc where it was and the next
    # run re-covers the window — the primary keys absorb the repeat. The
    # reverse order would advance past mail that never got stored, which
    # is the one failure mode ADR-006 calls catastrophic.
    #
    # try/finally rather than `with`: on a sqlite3 connection, `with` is a
    # TRANSACTION context manager — it commits or rolls back and leaves the
    # connection open. Worth knowing before the server holds one per request.
    conn = store.connect()
    try:
        store.save_emails(conn, emails, run_id)
        store.save_extractions(conn, extractions, failures, run_id)
        store.save_run(conn, run_id, coverage, digest,
                       ok_count=len(extractions), failed_count=len(failures),
                       started_at=now)
    finally:
        conn.close()

    # state saved LAST: if anything above crashed, the next run simply
    # re-covers the same window and dedupe absorbs it (ADR-006 —
    # duplicates cheap, gaps catastrophic)
    state["seen_ids"] += [e.id for e in emails]

    if coverage.truncated:
        # ADR-015, the binary rule. The window was not fully read, so it
        # is not finished: hold the watermark and let the next run re-query
        # the SAME window. Advancing to a "last covered" timestamp instead
        # would be unsound — Gmail's list order follows the untrusted
        # Date: header (ADR-011), so mail newer than any such boundary can
        # still have been cut, and advancing past it loses it for good.
        #
        # Written back rather than left alone, because "leave it alone" is
        # wrong on a first run: last_run_utc is None there, `since` comes
        # from now - FIRST_RUN_LOOKBACK, and a later run would recompute it
        # against a later `now` — sliding the window forward and dropping
        # the oldest unread mail out of it. since + OVERLAP reproduces this
        # exact `since` next time, and is a no-op once a watermark exists.
        state["last_run_utc"] = (since + OVERLAP).isoformat()
        print(f"[watermark] held at {state['last_run_utc']} — "
              f"{coverage.missed} email(s) still unread in this window",
              file=sys.stderr)
    else:
        state["last_run_utc"] = now.isoformat()

    save_state(state)

    return RunResult(run_id=run_id, run_dir=run_dir, digest=digest,
                     extractions=extractions, failures=failures,
                     coverage=coverage)


def main() -> None:
    """The scheduled entrypoint: ingest, then deliver locally (ADR-014)."""
    try:
        result = ingest()
    except ReauthorizationRequired as e:
        print(f"[auth] {e}", file=sys.stderr)
        toast("Secretary: re-authorization needed",
              "Gmail access expired. Run `python connectors.py` in a "
              "terminal to re-consent.")
        sys.exit(2)          # nonzero -> visible in Task Scheduler's
                             # Last Run Result, not just buried in the log

    deliver(result)


if __name__ == "__main__":
    main()
