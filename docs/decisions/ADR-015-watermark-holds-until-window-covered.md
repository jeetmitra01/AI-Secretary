# ADR-015: The watermark advances only on a fully covered window

Date: 2026-08-07
Status: accepted

## Context

ADR-012 made truncation *visible* to the agent and explicitly left the
other half undone: "This discloses truncation; it does not prevent it."
The digest path never got even the disclosure — `run_digest.py` called
`fetch_since(since, max_results=100)` and never called `count_since` at
all.

That turns a disclosure gap into permanent data loss, because the digest
also owns the watermark:

    emails = fetch_since(since, max_results=100)   # newest 100 of N
    ...
    state["last_run_utc"] = now                    # advances regardless

On a window holding more than 100, the run reads 100 and then moves the
watermark to `now`. The next window starts at `now - 5min`. The
un-read remainder sits below that boundary and no future window will
ever include it. It is gone, and nothing says so.

ADR-006's overlap protects against gaps in *time*. Nothing protected
against gaps in *volume*, and dedupe-by-id quietly assumes the fetch was
complete for its window — which is exactly the assumption truncation
breaks.

This is not hypothetical. The 2026-08-07 16:44 run pulled 57 emails in a
16-hour window. The trigger is `-StartWhenAvailable` (SCHEDULING.md): a
machine closed over a weekend produces one catch-up run covering ~60
hours, comfortably past 100. The flag that makes the schedule reliable
is the flag that makes truncation likely.

## Decision

Two rules.

**1. The watermark is binary.** A run advances `last_run_utc` to `now`
only when it read the entire window. If anything was left unread, the
watermark is re-anchored to reproduce the same `since` next run.

**2. Dedupe moves into listing.** `fetch_since` and `count_since` both
take `skip_ids`, applied while paging ids and before any body is
fetched. `run_digest` passes `seen_ids` to both.

`Coverage(total_matching, fetched)` carries the numbers into the digest
header and the toast. Both are computed in code from connector counts,
never by the model — the same rule `composition.py` already applies to
`stats_line`.

## Why

**Why binary, and not "advance to the oldest email covered".** That was
the first proposal and it fails twice over:

- *It cannot make progress.* The Gmail query is `after:X`, open-ended
  forward, newest-first, capped. Lowering the watermark re-requests the
  same newest 100, all of which are already in `seen_ids`, so the run
  produces nothing and the watermark never recovers. The backlog is
  permanently unreachable and every digest reads "No new email."
- *The boundary is not real.* ADR-011: Gmail's list order tracks the
  sender-supplied `Date:` header, observed ~8 hours out of step with
  `internalDate`. So mail *newer* than "the oldest one I covered" can
  also have been cut. Advancing to that timestamp loses it.

The binary rule sidesteps both. It asks one question the connector can
answer exactly — was there anything left? — and it depends on no
ordering guarantee whatsoever, which is the right place for correctness
to live.

**Why skip_ids belongs in the connector.** Filtering after the fetch
cannot drain a backlog: the cap is applied when selecting ids, so a
post-hoc filter just deletes rows from the same fixed set. Skipping
during listing is also cheap — `messages.list` returns ids only, 500 per
call, while the expense is one `messages.get` plus one extraction call
per email.

**Why count before fetch.** Mail arriving between the two calls should
never look like a shortfall. Counting first can only make `fetched`
exceed `total_matching`, which `Coverage.missed` clamps to zero — a
complete window. Fetching first would invent a phantom backlog and pin
the watermark on it.

## Rejected

- **Raise `max_results` until it fits.** Extraction is sequential at
  ~4s/email (measured: 57 emails, ~3.8 min), so ~220 emails exceeds the
  15-minute `ExecutionTimeLimit` and Task Scheduler kills the run. State
  saves last, so nothing is lost — but no digest is ever produced and it
  fails identically every time. Trades a silent data gap for a silent
  timeout.
- **Page the whole window regardless of size.** Same wall, and it hits
  hardest on precisely the catch-up runs that need to succeed.
- **Advance the watermark and record the skipped ids as a backlog list.**
  Works, but adds a second piece of durable state that can disagree with
  the watermark. Holding the watermark keeps one source of truth.

## Consequences

- A truncated run leaves the window open, so the next scheduled run
  re-queries it and drains the next `MAX_FETCH`. Convergence takes as
  many runs as the backlog needs.
- **If arrivals persistently exceed `MAX_FETCH` per run, the watermark
  never advances and the window grows without bound.** The digest header
  and the toast both show the backlog, so this is visible rather than
  silent — but it is a real failure mode, and narrowing the query (the
  volume question deferred at the end of ADR-011) is what makes it
  unlikely.
- `MAX_SEEN_IDS = 5000` trimming interacts with a held watermark: ids
  trimmed while still inside an open window get re-fetched and
  re-summarized. Duplicates, not gaps — the direction ADR-006 chose.
- Every provider's `fetch_since`/`count_since` now takes `skip_ids`. The
  `OutlookConnector` stubs carry the note that Graph's `$count` cannot
  subtract them, so a non-empty `skip_ids` means paging ids there too.
- `run_digest` no longer post-filters by `seen_ids`; passing them to the
  connector is now the only dedupe. Passing `skip_ids` to one call and
  not the other would silently compare two different populations, which
  is why the ABC docstring names them together.
