"""
One-shot import of the existing runs/ archive into the ADR-019 store, so
the service has history on its first start instead of an empty database.

    python backfill.py            # import
    python backfill.py --dry-run  # say what it would do

What comes across: every run's coverage numbers, its digest markdown, and
its extractions. What cannot: the email bodies. They were never persisted
by any run before ADR-019 — they existed only inside a process that has
long since exited. Those extractions land as rows with no email to join
to, which is exactly what they are: a summary of mail we no longer hold.

Re-running is safe. Every write is an upsert keyed by run id or email id.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import store
from composition import Coverage
from console import use_utf8
from extraction import Extraction, ExtractionFailure

BASE = Path(__file__).parent
RUNS_DIR = BASE / "runs"


def parse_run_id(run_id: str) -> datetime | None:
    """Directory names are the run's local timestamp: 2026-08-11-0019."""
    try:
        return datetime.strptime(run_id, "%Y-%m-%d-%H%M").astimezone()
    except ValueError:
        return None


def backfill(dry_run: bool = False) -> None:
    run_dirs = sorted(d for d in RUNS_DIR.iterdir() if d.is_dir())
    if not run_dirs:
        print(f"nothing to import: {RUNS_DIR} is empty")
        return

    conn = store.connect()
    try:
        runs = extractions_total = failures_total = skipped = 0

        for run_dir in run_dirs:
            data_file = run_dir / "extractions.json"
            if not data_file.exists():
                print(f"  {run_dir.name}: no extractions.json — skipped")
                skipped += 1
                continue

            data = json.loads(data_file.read_text())
            cov = data.get("coverage", {})
            coverage = Coverage(
                total_matching=cov.get("total_matching", 0),
                fetched=cov.get("fetched", 0))

            # Validate on the way in rather than trusting the file. These
            # JSONs were written by older code, and a schema that has since
            # changed should fail loudly here, not silently store a shape
            # nothing can read — the same argument as extraction.py's
            # tripwire 2, applied to our own archive.
            ok, failed = [], []
            for raw in data.get("ok", []):
                try:
                    ok.append(Extraction.model_validate(raw))
                except Exception as e:
                    print(f"  {run_dir.name}: unreadable extraction "
                          f"{raw.get('id')!r}: {e}")
            for raw in data.get("failed", []):
                try:
                    failed.append(ExtractionFailure.model_validate(raw))
                except Exception as e:
                    print(f"  {run_dir.name}: unreadable failure record: {e}")

            digest_file = run_dir / "digest.md"
            digest = (digest_file.read_text(encoding="utf-8")
                      if digest_file.exists() else None)

            print(f"  {run_dir.name}: {len(ok)} ok, {len(failed)} failed, "
                  f"digest {'yes' if digest else 'MISSING'}"
                  + (f", {coverage.missed} missed" if coverage.truncated else ""))

            if not dry_run:
                store.save_extractions(conn, ok, failed, run_dir.name)
                store.save_run(conn, run_dir.name, coverage, digest,
                               ok_count=len(ok), failed_count=len(failed),
                               started_at=parse_run_id(run_dir.name))

            runs += 1
            extractions_total += len(ok)
            failures_total += len(failed)

        verb = "would import" if dry_run else "imported"
        print(f"\n{verb} {runs} run(s), {extractions_total} extraction(s), "
              f"{failures_total} failure(s)"
              + (f", {skipped} skipped" if skipped else ""))
        if not dry_run:
            print(f"store now holds: {store.counts(conn)}")
            print("note: 0 emails is expected — bodies predate ADR-019 and "
                  "start accumulating from the next ingest.")
    finally:
        conn.close()


if __name__ == "__main__":
    use_utf8()          # digests carry ⚠ and em-dashes (console.py)
    backfill(dry_run="--dry-run" in sys.argv)
