# ADR-011: Ordering of fetch_since results

Date: 2026-07-27 (decided 2026-08-07)
Status: accepted

## Context

`fetch_since` returns emails in whatever order Gmail's `messages.list`
yields. Observed in the smoke test:

    14:13:05  Fanatics
    05:41:53  Lensa        <- ~8 hours out of place
    13:32:20  Urban Outfitters
    13:47:29  MLB          <- later than the line above

The cause is a deliberate choice already made elsewhere in the
connector: `_normalize` uses `internalDate` (Gmail's authoritative
receipt time) rather than the `Date:` header, because senders lie about
`Date:`. But `messages.list` orders results largely *by* that same
untrusted header. So list order and `received_at` order diverge, and
Lensa's header is off by roughly eight hours.

Using `internalDate` is not in question — it is the correct source of
truth. The open question is what `fetch_since` promises about sequence.

The ABC contract documents three guarantees (plain-text bodies, UTC
timestamps, possible duplicates across overlapping calls). It says
nothing about ordering, so callers today may reasonably assume either
behavior. The summarizer is the caller most likely to assume recency.

## Decision

Option 1: the connector sorts by `received_at` descending before
returning, and the guarantee joins the three already in the ABC
docstring. Emails with no `received_at` sort last rather than raising.

## Why

- Every caller that has appeared so far wants recency. The digest reads
  newest-first, and when an extraction budget forces a partial run, the
  budget should be spent on the newest mail. Making each caller
  re-sort is a rule that gets forgotten exactly once.
- The connector already holds the full result set in memory, so the
  sort is free.
- Outlook's `$orderby=receivedDateTime` satisfies the same contract
  natively, so honoring it in a second provider costs nothing.

## What this decision explicitly does NOT fix

Sorting happens **after** truncation, and that ordering cannot be
repaired. `fetch_since` takes the first `max_results` ids in Gmail's
list order, then sorts *those*. Since list order tracks the untrusted
`Date:` header, the ids that get cut are not the oldest by
`internalDate` — they are the oldest by what senders claimed, which the
smoke test showed diverging by ~8 hours.

Making the cut correct would require knowing every message's
`internalDate` before choosing which to keep, and `messages.list`
returns ids only — the timestamp arrives with `messages.get`, which is
the expensive call truncation exists to avoid. There is no cheap fix.

The consequence is load-bearing for ADR-012's unfinished half: on a
truncated fetch, the covered set is fuzzy at its lower edge by roughly
the header skew. **Any watermark scheme that advances to "the oldest
email I actually covered" is therefore unsound** — mail newer than that
boundary may still have been cut, and advancing past it loses that mail
permanently. A correct watermark rule must be binary: the window was
either fully covered (advance) or it was not (do not advance). That
rule needs no ordering guarantee at all, which is the right place for
correctness to live.

## Rejected

- **Document "order is unspecified" and let callers sort.** Keeps the
  connector thin and honest, but pushes an easy-to-forget obligation
  onto every present and future caller for no saving — the sort is one
  line over data already in hand.
- **Sort by the `Date:` header** to match Gmail's own list order:
  consistent, but consistent with the untrusted value ADR-008 already
  rejected as a source of truth.

## Consequences

- The ABC contract gains a fourth guarantee; `OutlookConnector` must
  honor it when built.
- Callers may assume `emails[0]` is the most recently received.
- Ordering is a *presentation* guarantee only. It does not make a
  truncated fetch complete, and no caller may treat it as evidence of
  coverage — that is `count_since`'s job (ADR-012).

## Related, not decided here

Truncation is only reached because of volume, and volume is mostly
noise: of the 57 emails in the 2026-08-07 16:44 run, 53 extracted as
`automated`. Narrowing the connector query (e.g. excluding Gmail's
promotions/social categories) would cut how often truncation is hit at
all, and would cut token spend on every run. That is a change to what
"the inbox" means for this agent, not an ordering or coverage fix, so
it belongs in its own ADR rather than being smuggled into this one.
