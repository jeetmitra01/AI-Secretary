# ADR-018: Add calendar.readonly, on one token, via scope-preserving consent

Date: 2026-08-12
Status: accepted

## Context

`check_calendar` has returned `[]` since the first agent loop. Phase 2
(meeting detection + calendar cross-reference) cannot exist without it,
and the digest is already surfacing meeting requests it cannot check
availability against — the recruiter's "Monday at 2:30pm EST" is
reported with no idea whether that slot is free.

ADR-003 restricted v1 to `gmail.readonly` and stated the principle:
security by capability restriction, not prompt wording. This is the
first widening of that grant, so it is the first real test of whether
the principle was about *this specific scope* or about *how scopes are
chosen*.

## Decision

Request `https://www.googleapis.com/auth/calendar.readonly` — read only,
no write scope, no `events` write, no `calendars` write.

**One token file**, shared with Gmail. Re-consent requests the **union**
of the scopes needed and the scopes already granted.

Calendar sits behind its own `CalendarConnector` ABC, mirroring ADR-002:
the agent gets `busy_blocks(day, tz)` and never touches a provider API.

## Why

**Why read-only is still the whole safety story.** Writing to a calendar
is how an injected instruction in an email body ("cancel all his
meetings") becomes damage. Without a write scope, the worst a successful
injection achieves is a wrong answer in a digest — recoverable, and
visible. The capability simply does not exist to be abused.

**Why one token, not one per capability.** Splitting tokens is the more
obviously "secure" choice and it was rejected deliberately. The
restriction that actually binds the model is the **tool registry** — the
model can only invoke the Python functions we register, regardless of
what the token could theoretically reach. An auth-layer split would
therefore buy very little against the real threat (injection steering
tool use) while doubling the 7-day Testing-mode re-consent chore, which
is already the most likely cause of a silently dead schedule. Both
scopes are read-only, so a leaked token's blast radius is similar either
way. If that posture ever changes, the split is a one-line change:
`GoogleCalendarConnector(token_file="token_calendar.json")`.

**Why consent must request the union.** Adding a scope by consenting for
*only* that scope returns a token holding *only* that scope. Gmail would
have broken the instant Calendar was added — a capability silently
removed as a side effect of adding one. `_renew` now unions the
requested scopes with those already on the token, and says so on stderr.

**Why the sufficiency check exists.** `creds.valid` says nothing about
scopes. A `gmail.readonly` token stays valid forever while every Calendar
call returns 403 "Request had insufficient authentication scopes", which
reads like a broken grant rather than "consent again". The check names
the cure at the point of failure and skips a refresh round trip that
could never have helped.

There is a trap inside the trap, found by testing: passing `scopes=` to
`Credentials.from_authorized_user_file` makes the object report the
scopes you *asked for* as though they were granted. The sufficiency
check compared a set against itself and was inert until the argument was
removed. Anything that reads `creds.scopes` to learn what was granted
must construct the credentials without a `scopes` argument.

## Rejected

- **Any calendar write scope.** ADR-003 stands. Drafts and writes are
  phase 4 at the earliest, and get their own ADR.
- **`freebusy.query` instead of `events.list`.** Cheaper, merges
  overlapping blocks for free, and respects transparency without extra
  work — but it returns no event titles. "You are busy 15:00-16:00" is
  much less useful to the principal than "Board prep", and the existing
  tool schema already promised a title.
- **A separate token per capability.** See above; friction outweighs the
  benefit for two read-only scopes.
- **Deriving availability from the digest's extracted meeting times.**
  That is inference over the model's own output; the calendar is ground
  truth and is cheap to ask.

## Consequences

- A one-off re-consent is required, and the Google Calendar API must be
  enabled in project `execassistant-503715`. Until then, Calendar calls
  raise `ReauthorizationRequired` with the missing scope named.
- The scheduled digest is unaffected: it never calls `check_calendar`,
  and `load_credentials(GMAIL_SCOPES)` is satisfied by the widened token.
- An empty list means **free**. Lookup failure raises. Conflating the two
  would let a broken calendar read as a clear day, which is the same
  class of silent-negative error as ADR-012's "0 meeting requests".
- Declined events, events marked Free, and cancelled events are excluded
  before the model sees them, so everything returned is a real conflict.
  Recurrence is expanded server-side (`singleEvents=True`) — without it a
  weekly standup returns once and the agent reads you as free every week
  but the first.
- Multiple calendars are supported via `calendar_ids`, defaulting to
  `("primary",)`. Each block is tagged with the calendar it came from.
