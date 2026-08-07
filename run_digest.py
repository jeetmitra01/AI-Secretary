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
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

BASE = Path(__file__).parent
load_dotenv(BASE / ".env")                      # before Anthropic() — the
                                                # import-order lesson, encoded
from anthropic import Anthropic                 # noqa: E402
from connectors import CONNECTORS, ReauthorizationRequired  # noqa: E402
from extraction import Extraction, ExtractionFailure, extract_email  # noqa: E402
from composition import Coverage, compose_digest, toast_summary      # noqa: E402

# The digest carries "⚠" and em-dashes, and a Windows console still
# defaults to cp1252, where printing those raises UnicodeEncodeError —
# killing the run AFTER the digest file is written but BEFORE the toast
# fires, so the one channel that reaches you is the one that dies. The
# file is already written utf-8 explicitly; this makes stdout agree.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):          # None under pythonw.exe
        _stream.reconfigure(encoding="utf-8", errors="replace")

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


def deliver(digest: str, extractions: list[Extraction], run_dir: Path,
            coverage: Coverage | None = None) -> None:
    digest_file = run_dir / "digest.md"
    digest_file.write_text(digest, encoding="utf-8")
    print(digest)                                  # console always works

    title, line = toast_summary(extractions, coverage)
    toast(title, line, launch=str(digest_file.resolve()))  # click -> digest


# --- orchestrator ----------------------------------------------------------

def main() -> None:
    client = Anthropic()
    # No allow_interactive_auth: this entrypoint is the scheduled one, so
    # a dead token must raise, never try to open a browser.
    connector = CONNECTORS[EMAIL_PROVIDER]()
    state = load_state()

    now = datetime.now(timezone.utc)
    since = (datetime.fromisoformat(state["last_run_utc"]) - OVERLAP
             if state["last_run_utc"] else now - FIRST_RUN_LOOKBACK)
    seen = set(state["seen_ids"])

    try:
        # Count BEFORE fetching, and pass the SAME skip_ids to both so the
        # two numbers describe one population. Counting after would let
        # mail arriving between the calls look like a shortfall, which
        # would pin the watermark below on a backlog that isn't there.
        pending = connector.count_since(since, skip_ids=seen)
        emails = connector.fetch_since(since, max_results=MAX_FETCH,
                                       skip_ids=seen)
    except ReauthorizationRequired as e:
        # State is deliberately NOT saved: the watermark stays put, so once
        # you re-consent the next run re-covers this whole window (ADR-006).
        print(f"[auth] {e}", file=sys.stderr)
        toast("Secretary: re-authorization needed",
              "Gmail access expired. Run `python connectors.py` in a "
              "terminal to re-consent.")
        sys.exit(2)          # nonzero -> visible in Task Scheduler's
                             # Last Run Result, not just buried in the log

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
    run_dir = RUNS_DIR / datetime.now().strftime("%Y-%m-%d-%H%M")
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "extractions.json").write_text(json.dumps(
        {"coverage": {"total_matching": coverage.total_matching,
                      "fetched": coverage.fetched,
                      "missed": coverage.missed},
         "ok": [e.model_dump() for e in extractions],
         "failed": [f.model_dump() for f in failures]}, indent=1))

    deliver(digest, extractions, run_dir, coverage)

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


if __name__ == "__main__":
    main()
