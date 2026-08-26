# ADR-028: Extraction runs concurrently, in input order

Date: 2026-08-26
Status: accepted

## Context

ADR-016 closed by naming the lever it was not pulling:

> **The real lever is throughput, not exclusion.** Extraction runs
> sequentially at ~4s/email, which is what caps `MAX_FETCH` at 100
> inside a 15-minute `ExecutionTimeLimit`. Concurrency, and a cheap
> first-pass triage model before full extraction, are the changes that
> raise the ceiling without creating a blind spot. Both are open.

Measured on 2026-08-26, run `2026-08-26-1407`: 100 emails, **6 min
58 s** wall clock, 0 failures. The window held 980 matching emails, so
880 went unread and ADR-015 held the watermark — correctly, and for the
fifth run running. The watermark has not moved since 2026-08-10.

At two runs a day the backlog drains at ~138/day against ~62/day
arrivals: about a week. Every one of those runs spends ~7 minutes
blocked on a network round trip it could have overlapped.

The 4s is not compute. `extract_email` sends one request and waits.

## Decision

`run_digest.extract_all()` runs extraction across a
`ThreadPoolExecutor` of `EXTRACT_WORKERS = 4`, sharing the one
`Anthropic` client the caller passed.

**Results are returned in input order**, via `pool.map`, which yields by
input position rather than by completion.

Three things deliberately NOT changed:

- **`MAX_FETCH` stays at 100.** Raising it belongs in its own decision,
  made against a measured wall time from `digest.log`, not against an
  estimate. ADR-015 rejected "raise `max_results` until it fits"
  precisely because it was ungrounded, and that objection does not stop
  applying just because the number is now bigger.
- **No retry or backoff.** `extract_email` keeps its "never raises"
  contract: a 429 becomes one `ExtractionFailure` row, visible in the
  digest, exactly as today. Four workers is conservative enough that
  rate limiting is unlikely, and inventing a retry policy before seeing
  a single 429 would be tuning against an imagined failure.
- **`extraction.py` is untouched.** It stays one-email-in, one-out
  (ADR-009). Concurrency is an orchestration property and lives with
  the orchestrator.

## Why

**Why 4 and not 8 or 16.** The ceiling here is the Anthropic rate limit,
not the machine, and the account's limit is shared with `/chat` and
`/chat/stream` (ADR-025) which a human may be using while a scheduled
run is in flight. 4 takes ~7 minutes to ~2. That is already the
difference between "drains in a week" and "drains in two runs", and the
remaining gain from 8 or 16 buys less than the risk of starving the
interactive path.

**Why input order is load-bearing and not cosmetic.** `fetch_since`
returns newest-first, and ADR-011 explains what that guarantee cost to
establish: Gmail's list order tracks the sender-supplied `Date:` header,
observed ~8h out of step, so the connector re-sorts on `internalDate`.
The digest inherits that order. Returning results as they complete would
throw it away and reorder the digest by whatever sequence the API
happened to answer in — a silent regression, since the digest would
still be *correct*, just no longer newest-first. `as_completed()` is the
obvious "improvement" a future edit reaches for, so a test guards it.

**Why sharing one client is safe.** The Anthropic SDK is built on
`httpx`, whose client is designed for concurrent use. Constructing four
clients would also mean four connection pools, which is the opposite of
what we want.

**Why a named function rather than an inline loop.** The ordering
guarantee needs something to be tested against. `tests/
test_ingest_concurrency.py` monkeypatches `extract_email`, which is only
possible because `extract_all` takes the client as an argument instead
of reaching for a module-level one.

## Rejected

- **8 or 16 workers.** See above; the interactive path shares the quota.
- **`as_completed()` with a re-sort afterwards.** Same result, more
  code, and the re-sort key would have to be reconstructed from the
  email list anyway. `map` gets it for free.
- **`asyncio` with the async client.** The correct answer for thousands
  of calls. For 100 blocking calls in a scheduled script it means
  making `ingest()` async, which propagates to `server.py`'s handlers
  and to the graph — a large change to buy nothing at this scale.
- **Retry with exponential backoff in the worker.** Deferred, not
  refused. Revisit when `digest.log` shows a real 429, and note that at
  16 workers it would have been mandatory rather than optional.
- **Batching N emails into one request.** Fewer round trips, but it
  breaks tripwire 1 (the per-email id echo) into a per-item check inside
  a shared response, and one malformed object then costs N extractions
  instead of 1. ADR-009's failure isolation is worth more than the
  round trips.

## Consequences

- A full `MAX_FETCH = 100` run should drop from ~7 minutes to ~2. The
  `PT1H` execution limit in `SCHEDULING.md` now has real headroom, and
  raising `MAX_FETCH` becomes a measurement away rather than a gamble.
- Per-email progress lines still print in input order, so `digest.log`
  reads the same as before. They now appear in bursts as the map
  advances, rather than at a steady ~4s cadence.
- The run is no longer trivially interruptible mid-way: killing the
  process now abandons up to 4 in-flight calls instead of 1. Cost only;
  ADR-006 already makes a lost run cost a repeat, not a gap.
- Cost per run is unchanged. This buys wall time, not tokens. The token
  lever is the pre-triage pass, still open.
- Concurrency is now a property `server.py` inherits, because
  `POST /ingest` calls the same `ingest()`. An API-triggered run and a
  scheduled one still cannot drift (the ADR-020 rule).
