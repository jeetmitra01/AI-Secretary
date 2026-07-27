# ADR-012: Surface fetch truncation instead of hiding it

Date: 2026-07-27
Status: accepted

## Context
`fetch_since` returns the newest `max_results` matches and says nothing
about the rest. Running `secretary_agent.py` against a live mailbox, the
window held 67 emails, the tool returned 20, and the digest opened with
"your inbox summary for the last 24 hours — 20 emails, 0 meeting
requests." The IDs it cited spanned about 90 minutes.

The summary was not wrong about the emails it saw. It was wrong about
its own scope, and it had no way to know: the tool handed back 20 items
with no signal that 47 more matched. The damaging part is the negative —
"0 meeting requests" asserted over 30% of the data, for the exact
question the user asked.

ADR-006 buys gap-free fetching *between* runs. Nothing protected against
the gap *within* a run, which is the same silent-loss failure that ADR
says is the worst one for a secretary.

## Decision
`count_since(dt) -> int` joins the `EmailConnector` contract (ADR-002):
how many emails fall in the window, ignoring `max_results`. It lists ids
and never fetches bodies.

`fetch_recent_emails` now returns an object — `emails`, `returned`,
`total_matching`, `truncated`, `window_start_utc`, `covered_from_utc`,
`covered_to_utc` — and the tool description tells the model to disclose
truncation, quote the covered span, and scope any "none found" to that
slice.

The covered span is separate from `window_start_utc` on purpose. The
first version shipped only the window start, and the model quoted it as
the range it had looked at — reporting ~20 hours of coverage for a
3-hour slice. The requested window and the delivered window are
different facts, and a truncated fetch is exactly when they diverge, so
both are named and the description says which one to quote.

## Why
The model cannot caveat what it cannot see. Making the shortfall a
number in the tool result puts the disclosure where the model will act
on it, rather than relying on a system-prompt rule about a condition it
has no evidence for.

Counting is cheap next to fetching: one list call per 500 ids and no
`messages().get()` at all, against 20+ per-message round trips.

## Rejected
- Raise `max_results` to cover the window: 67 emails at 1500 chars is
  ~25k tokens of untrusted body text per fetch, and it fails again the
  first busy day. Truncation is the right behavior; silence was the bug.
- Over-fetch by one to detect truncation: cheaper, but yields a boolean.
  The count is what makes the caveat useful to the reader.
- Count in the agent via the Gmail service directly: one call, no
  contract change, but the agent would touch a provider API — the thing
  ADR-002 and the `models.py` docstring exist to prevent.
- Gmail's `resultSizeEstimate`: an estimate that drifts from the real
  count on large mailboxes. A wrong number is worse than none here,
  since its whole purpose is to be quoted to the user.

## Consequences
- Every provider must now implement `count_since`. `OutlookConnector`
  carries a `NotImplementedError` stub rather than omitting it, so the
  class stays instantiable and `CONNECTORS["outlook"]()` still fails at
  the point of use with a useful message instead of a bare TypeError at
  construction.
- One extra list round trip per fetch, on every call.
- `truncated` compares against what `fetch_since` returned, not against
  the post-dedupe list — otherwise a re-seen email would be reported as
  missing mail.
- This discloses truncation; it does not prevent it. The digest still
  covers only a slice unless the caller pages or narrows the window.
  Paging is left to the model via `max_results` / `hours_back`, which
  means a busy window costs multiple turns.
