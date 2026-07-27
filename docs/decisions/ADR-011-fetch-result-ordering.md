# ADR-011: Ordering of fetch_since results

Date: 2026-07-27
Status: open

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

Not yet made. Two candidates:

1. Sort by `received_at` descending before returning. Callers get the
   intuitive guarantee; cost is that the connector must hold the full
   result set (it already does) and the guarantee must hold for every
   future provider, including Outlook.
2. Document "order is unspecified" in the ABC contract and let callers
   sort. Keeps connectors thin and honest about what providers give,
   at the cost of every caller remembering to sort.

## Why it matters now

Whichever way this goes belongs in the ABC docstring next to the
existing three guarantees, before a second provider and a second caller
exist. Outlook's `$orderby=receivedDateTime` would satisfy option 1
natively, so the contract is cheap to honor there — but that is an
argument to decide now, not to defer.
