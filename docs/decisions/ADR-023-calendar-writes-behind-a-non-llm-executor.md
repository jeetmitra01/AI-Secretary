# ADR-023: Calendar writes live behind a non-LLM executor, in a separate trust zone

Date: 2026-08-23
Status: accepted
Amends: ADR-003, ADR-018

## Context

ADR-003 made v1 read-only and named the reason: an injected model with
write powers causes real damage, and capability restriction is the only
defense that holds when prompting does not. ADR-018 widened the grant to
`calendar.readonly`, and explicitly rejected "any calendar write scope —
drafts and writes are phase 4 at the earliest, and get their own ADR."

This is that ADR. The digest already detects meeting requests it cannot
act on. The first act we want is: create the event.

Two shapes were on the table.

**One agent, one more tool.** Register `create_calendar_event` next to
`fetch_recent_emails`. Simple, and it puts a write capability in the same
tool registry as the tool that reads untrusted email bodies. An injected
instruction inside a body then has a live path to the calendar. ADR-004
says we cannot prompt our way out of that.

**Two zones.** The agent that reads email proposes; a separate component
that never reads email commits. That is what we chose.

## Decision

Three parts, and all three are load-bearing.

**1. A write scope on a separate token.** Request
`https://www.googleapis.com/auth/calendar.events`, stored in
`token_write.json` — NOT the shared `token.json` that Gmail and
`calendar.readonly` use. Only `executor.py` loads it.

**2. The reading agent never holds the write tool.** Its tool is
`propose_calendar_event`, which validates a payload, writes a row to the
`proposals` table, and returns an id. It cannot reach Google. The write
function is absent from `TOOL_FUNCTIONS`, and a test asserts that.

**3. The executor is deterministic Python, not a second LLM.** It takes a
proposal id, re-loads the payload from SQLite, re-validates it against the
schema, re-runs the policy gate (ADR-024), and only then calls the API.

The capability, in this first version, is deliberately smaller than the
scope permits: create only, one event, primary calendar,
`status: "tentative"`, **no attendees**, no update, and no delete by the
model.

## Why

**The channel is the boundary, not the process split.** A second agent
buys nothing if what crosses between them is prose. Free text derived from
an email body carries the injection across intact, and the executor
becomes a confused deputy with better branding. The boundary is real only
because what crosses it is a validated `CalendarProposal` — typed fields,
absolute datetimes, no free-form instruction channel — and because the
executor never reads a body. This is the same discipline `extraction.py`
already uses: a schema is a tripwire, not decoration.

**Why the executor is not an LLM.** We removed deletion and attendees from
scope, so ask what judgment is left: validate, check policy, call the API.
That is code. An LLM there would add a second prompt surface to attack,
plus latency, cost, and nondeterminism, and would decide nothing that a
function cannot. The seam is built as though it were an agent — its own
module, its own token, a typed input contract — so that promoting it to a
real agent later (choose the best free slot among three) changes only what
lives behind the seam.

**Why a separate token, when ADR-018 argued against splitting.** ADR-018
rejected the split because both scopes were read-only, so a leaked token's
blast radius was similar either way, and it said the split becomes a
one-line change "if that posture ever changes." It just changed. The
scheduled digest and the chat agent keep the read-only token, so the
unattended path never holds a credential that can write.

**Why no attendees.** Adding an attendee makes Google send that person an
invitation email. That is outbound mail through a side door, and "no
sending" is an invariant of this system, not a preference. Invitations
belong with phase 4's draft-and-send policy and its own ADR.

**Why tentative, and why the marker.** Every event the executor creates is
`tentative` and carries a private extended property naming this system and
the proposal id. Tentative makes a wrong event obvious in the UI rather
than authoritative. The marker is what makes undo possible without giving
anything a general delete power: the human-only undo path can only touch
events that carry it.

**Why create-only when the scope allows more.** No create-only Calendar
scope exists. The restriction therefore lives in our code, not in the
grant. Saying so plainly is the point — see Consequences.

## Rejected

- **One agent with a write tool.** The whole reason for the ADR. It puts
  the capability in the same registry as untrusted input.
- **An LLM executor gated on the secretary's stated confidence.** See
  ADR-024: self-reported confidence is not calibrated and is influenced by
  the attacker, so it cannot be the authority. It survives as one
  condition among deterministic ones.
- **A free-text handoff between the two zones.** Defeats the split.
- **The full `calendar` scope.** It can delete entire calendars. `events`
  is the smallest scope that can create one.
- **Attendees in v1.** Outbound email, as above.
- **Delete as a model tool.** Out of scope by request and by judgment. The
  undo path is human-only and marker-scoped.
- **Auto-commit on day one.** ADR-003 says trust is earned against
  observed accuracy. ADR-024 is how we measure it first.

## Consequences

- **OAuth stops being the whole safety story.** `calendar.events` permits
  update and delete on every event we own. Nothing in the grant prevents
  it; only our code does. That is a strictly weaker guarantee than the
  system had yesterday, and it is the real price of this feature.
- **A second 7-day Testing-mode re-consent.** `token_write.json` dies on
  its own schedule. Its death is silent until a confirm fails with
  `ReauthorizationRequired`, which is why the confirm path names the cure.
- **Normalization moves into the model.** `extraction.py` keeps proposed
  times verbatim on purpose; something must still turn "Monday at 2:30pm
  EST" into an absolute instant. The agent does it, so a confident
  timezone misread is now possible. The mitigation is cheap and required:
  every proposal stores `source_time_text` verbatim, and the confirmation
  view shows the verbatim phrase next to the normalized instant so a human
  compares them.
- **CLAUDE.md's READ-ONLY invariant is amended, not repealed.** Read-only
  for the model, still. The write happens on a human-confirmed path, in a
  component the model cannot call.
- **The `proposals` table holds email-derived text.** ADR-004 is unchanged
  by that, exactly as ADR-019 said of stored bodies: storage is not trust.
- **`check_calendar` and the writer stay separate classes.** A read path
  cannot acquire write powers by inheriting them.
