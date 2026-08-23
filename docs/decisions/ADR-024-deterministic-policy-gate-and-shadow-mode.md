# ADR-024: A deterministic policy gate decides autonomy, and it runs in shadow mode first

Date: 2026-08-23
Status: accepted

## Context

ADR-023 splits proposing from committing. It leaves one question open:
what decides whether a proposal may ever commit without a human?

The obvious answer is the agent's own confidence. Add a `confidence` field
to the extraction or the proposal, auto-commit the "high" ones, queue the
rest. It is one line, and it is the wrong authority.

## Decision

A proposal is judged by **deterministic checks over the payload and over
our own stored data**, in `proposals.evaluate()`. The model's stated
confidence is one input among them, never the decision.

The gate runs at proposal time AND again inside the executor before the
API call. Both, not one.

Auto-commit ships **off**. `executor.AUTO_COMMIT = False`. Every proposal
goes to the human queue, and the verdict the gate would have reached is
recorded on the row without acting on it. That is shadow mode.

## Why

**Self-reported confidence is not calibrated.** It is a token the model
emits, not a measurement of its own accuracy. Treating it as a probability
because it is spelled like one is the error.

**Worse, the attacker can move it.** The input to the extraction and to
the proposing turn is the email body. Text in that body can push the model
toward stating certainty. A gate whose threshold is partly controlled by
the person who wrote the email is not a gate. This is exactly the failure
ADR-003 exists to prevent, rebuilt one layer up.

**Deterministic checks are facts we own.** Whether the sender has written
before is a query against `emails`. Whether the slot is free is a call to
the read-only calendar. Whether the time is inside a sane window is
arithmetic. None of these can be argued with by a paragraph of email text.

The checks, and what each one is actually defending against:

| check | defends against |
| --- | --- |
| `schema` | malformed or non-absolute times; missing tz |
| `future` | an event proposed into the past |
| `lead_time` | "right now" bookings and far-future junk (1h to 14d) |
| `duration` | a 9-hour "quick sync" (15 to 120 min) |
| `working_hours` | a 3am meeting from a timezone misread |
| `known_sender` | a stranger's first email steering the calendar |
| `no_conflict` | double-booking; uses the ADR-018 read path |
| `confidence` | the model's own doubt, demoted to a veto only |
| `not_flooding` | one email spawning many proposals |

`known_sender` and `no_conflict` are the two that need our data rather
than the payload, and they are the two an injected body cannot fake.

**Why the gate runs twice.** A check that only ran when the proposal was
written is a check that can be outrun: the calendar can fill, the token
can die, the clock moves a pending proposal out of its window. The
executor re-runs everything and fails closed. The proposal-time run exists
only to record a verdict and to tell the model something useful.

**Why shadow mode, and what it produces.** ADR-003 promised autonomy "only
after weeks of observed accuracy," which is a measurement nobody had a way
to take. Now there is one. After some weeks, ask the table: of the
proposals the gate called eligible, how many did a human confirm unchanged?
That number is a real accuracy estimate over our own traffic. Turning
`AUTO_COMMIT` on is then a decision with evidence behind it — and it gets
its own ADR, recording the number.

## Rejected

- **Model confidence as the gate.** Above. It stays as a veto: `low`
  confidence blocks eligibility, `high` alone never grants it.
- **An LLM judging the proposal.** A second model reviewing the first
  model's reading of an untrusted email inherits the same injection
  surface, at twice the cost, and produces an unauditable verdict.
- **Running the gate only in the executor.** Correct but silent — the
  proposing agent could not tell the user why something will need review.
- **Running the gate only at proposal time.** Fails open as state changes.
- **Auto-commit behind a config flag from day one.** The flag would be
  turned on early, by the person most convinced the system works.
- **A numeric score.** A single number invites a threshold argument and
  hides which check failed. A list of failed check names is auditable and
  is what the confirmation view shows the human.

## Consequences

- The policy is written once and imported by both the tool and the
  executor. Two copies would drift, and the drift would fail open.
- Every proposal row stores the verdict (`policy_ok`) and the failed check
  names (`policy_failed`), so shadow mode is queryable history rather than
  a log line.
- `no_conflict` makes the proposal path depend on the read-only calendar
  connector, so a dead `token.json` degrades proposals to "needs review"
  instead of silently passing. Failing closed is intended.
- The thresholds (1h–14d, 15–120 min, 07:00–21:00 local) are constants in
  `proposals.py`. Changing one is a policy change and belongs in a commit
  message, not a config file nobody reads.
- Pending proposals expire after 48 hours. A stale proposal that an
  injection planted cannot be confirmed by a tired human a week later.
