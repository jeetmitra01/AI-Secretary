# ADR-008: Platform timestamps over sender-supplied headers

Date: 2026-07-27
Status: accepted

## Context
An email carries two notions of "when": the sender-written `Date:`
header and the platform's own receipt record (Gmail `internalDate`,
Graph `receivedDateTime`). Sender Date headers are self-reported —
frequently wrong, oddly formatted, or in surprising timezones.

## Decision
`Email.received_at` always comes from the platform's receipt
timestamp, normalized to timezone-aware UTC. The sender's `Date:`
header is ignored for ordering and windowing.

## Why
Prefer data the untrusted party cannot control — the same principle
as ADR-004, applied to metadata. Fetch windows (ADR-006), digest
grouping, and "who asked first" ordering all depend on timestamps;
building them on attacker/sender-controlled input invites silent
misordering.

## Rejected
Parsing the `Date:` header: maximum compatibility with what the
sender *claims*, at the cost of trusting it.

## Consequences
- "Received at" can differ from what the sender's own client shows
  by seconds to minutes; acceptable for a digest.
- Timezone conversion for display happens at the edge (prompt/report
  layer, which knows the user's timezone) — storage stays UTC.
