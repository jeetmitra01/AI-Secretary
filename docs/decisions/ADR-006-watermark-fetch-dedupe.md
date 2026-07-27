# ADR-006: Overlapping fetch windows + dedupe by message id

Date: 2026-07-27
Status: accepted

## Context
The scheduler periodically asks "what arrived since last run?" Gmail's
`after:` query is epoch-second granular and coarse at boundaries;
Graph's `$filter` has its own edge behavior. Exact, gap-free
incremental fetching is not a guarantee either provider makes cheaply.

## Decision
Fetch from `last_run_time - overlap` (~5 minutes) and dedupe by
`Email.id` (a `seen` set / persisted id log). Connectors are allowed
to return duplicates across calls; callers must dedupe. This is part
of the `EmailConnector` contract (ADR-002).

## Why
Gaps lose email silently — the worst failure mode for a secretary,
invisible until a missed meeting surfaces. Duplicates cost a set
lookup. Choosing the cheap, visible failure over the expensive,
silent one.

## Rejected
- Exact watermark (`after: last_run`): boundary emails silently
  dropped by timestamp granularity.
- Provider push (Gmail watch/Pub-Sub, Graph webhooks): correct
  long-term, but infrastructure-heavy for a twice-daily digest and
  teaches nothing needed yet. Revisit if near-real-time is wanted.

## Consequences
- The seen-id record must eventually persist across process restarts
  (file or SQLite), not just live in memory.
- Contract tests assert: fetching twice with overlap yields no
  duplicates after caller-side dedupe.
