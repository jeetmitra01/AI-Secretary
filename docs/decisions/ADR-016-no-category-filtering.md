# ADR-016: No category filtering; a narrow marketing-subdomain denylist instead

Date: 2026-08-07
Status: accepted

## Context

ADR-015 leaves the digest correct but slow to converge: the connector
sees ~317 inbound emails per 72 hours against `MAX_FETCH = 100`, so a
catch-up run truncates and takes three runs to drain. ADR-011 closed by
noting the obvious lever — most of that volume is noise (53 of 57
emails in the 2026-08-07 16:44 run extracted as `automated`), and
Gmail already sorts noise into categories. Excluding
`promotions`/`updates` would cut ~92% of the window at a stroke.

That lever was measured before being pulled.

## The measurement

72-hour window, 317 emails after the existing inbound-only filter,
cross-tabulated against our own extraction labels (191 of them):

| category   | count | % window | meeting_request | needs_action | question |
| ---------- | ----: | -------: | --------------: | -----------: | -------: |
| promotions |   141 |    44.5% |               1 |            0 |        0 |
| updates    |   150 |    47.3% |               4 |            0 |        2 |
| social     |     6 |     1.9% |               0 |            0 |        0 |
| primary    |    20 |     6.3% |               0 |            5 |        3 |
| forums     |     0 |     0.0% |               0 |            0 |        0 |

**Every single meeting request in the window lives in `promotions` or
`updates`. Not one is in `primary`.**

The specific mail that would have been discarded (described by role,
not by name — ADR-030):

- **A recruiter, arriving on LinkedIn's `hit-reply@` relay** — 4 emails,
  0 of 4 automated, all in `updates`. This is the thread that produced
  the one genuinely time-sensitive item in the last two digests.
- **A leasing office, on a `knck.io` relay** — replying to an enquiry,
  `promotions`.
- **A second leasing office, on `assist.rent`** — apartment tour
  scheduling, `updates`.

The relay domains stay because they are the finding: the signal arrives
on bulk-sending infrastructure, which is exactly why a category filter
cannot separate it. Who was on the other end is not part of the argument.

## Decision

**Category filtering is rejected.** The connector query keeps every
category.

**A narrow denylist of bulk marketing subdomains is accepted** as a
modest, separately-justified volume trim — not as a fix for truncation.
The criterion is deliberately strict:

> Deny a *marketing subdomain*, never a brand. `e.usa.experian.com` and
> `m.sofi.org` are bulk-send subdomains; Experian fraud alerts and SoFi
> account notices originate elsewhere and stay visible. A denylist entry
> that could plausibly carry a security, financial, or legal notice does
> not go in.

## Why

The premise of the whole product is meeting detection (Phase 2). A
filter that removes 92% of the volume and 100% of the meeting requests
does not make the secretary cheaper — it makes it useless, and silently,
which is the exact failure mode ADR-012 and ADR-015 were written to
eliminate. Truncation at least announces itself now; a category filter
would not, and unlike truncation it is unrecoverable: filtered mail
never enters any window, so no watermark logic can ever reach it.

The result is counterintuitive enough to be worth stating plainly: for
this mailbox, Gmail's own idea of "unimportant" is anti-correlated with
ours. Recruiters route through LinkedIn, and leasing offices through
bulk senders, so both land in `updates`. The signal is *in* the noise
bucket.

## What the denylist actually buys

Measured over the same window: 317 → 249, a 21% cut. Over-match was
checked explicitly, because ADR-010's `-from:me` finding showed
`from:` matching envelope and VERP Return-Path addresses:

    linkedin.com  hit-reply@        (recruiter thread)   kept 4/4
    linkedin.com  inmail-hit-reply@ (recruiter InMail)   kept 1/1
    a leasing agent's direct address                     kept 8/8
    a leasing office's reply address                     kept 6/6

(The two LinkedIn relays are platform addresses — identical for every
user of the site — so they identify nobody and are kept verbatim. The
lower two were a named individual and a specific office this mailbox
dealt with, so they are described instead. `BULK_SENDERS` in
`connectors.py` keeps its real values: those are corporate broadcast
addresses, and they are live code that has to match. ADR-030 draws the
line.)

No keeper was lost. But 249 is still far above `MAX_FETCH = 100`, so
**this does not stop truncation and must not be mistaken for the fix.**

## Rejected

- **Exclude `promotions` + `updates`.** The table above.
- **Exclude `promotions` only.** Still costs a meeting request, for a
  44% cut. Same class of error, smaller discount.
- **Read `primary` only.** A 94% cut and the cleanest-looking option;
  it drops every meeting request in the window.
- **Denylist whole brands** (`zillow.com`, `experian.com`). Cheaper to
  maintain and catches more volume, but a fraud alert or a lease
  document would go with it. Subdomain granularity is the whole point.
- **Deny by `unsubscribe` header presence.** Tempting and general, but
  legitimate bulk-sent transactional mail carries it too — including
  the LinkedIn recruiter thread.

## Consequences

- Volume stays high by design; truncation remains normal on catch-up
  runs, handled (not prevented) by ADR-015.
- **The real lever is throughput, not exclusion.** Extraction runs
  sequentially at ~4s/email, which is what caps `MAX_FETCH` at 100
  inside a 15-minute `ExecutionTimeLimit`. Concurrency, and a cheap
  first-pass triage model before full extraction, are the changes that
  raise the ceiling without creating a blind spot. Both are open.
- The denylist is hand-maintained and will drift. It is a code constant
  so it shows up in review; `runs/` retains the evidence needed to
  re-derive it.
- Any future entry must be justified against the criterion above, in
  this ADR.
