# ADR-029: Extraction stays on Sonnet 5; Haiku 4.5 measured and rejected

Date: 2026-08-26
Status: accepted

## Context

`extraction.py` has carried this note since ADR-009:

> `MODEL = "claude-sonnet-5"` — try claude-haiku-4-5 later: extraction is
> a narrow task, and comparing the two models' error rates on your
> `runs/` archive is a free experiment in capability-vs-cost.

The experiment is now cheap to run for real. ADR-019 made the store the
record, so 397 emails have both a stored body to replay and a label to
score against. ADR-028 made a 400-call replay take under three minutes.

The prize looked good. List price, per million tokens:

| model | input | output | per 100 emails |
| --- | ---: | ---: | ---: |
| `claude-sonnet-5` | $3.00 | $15.00 | $0.558 |
| `claude-haiku-4-5` | $1.00 | $5.00 | $0.186 |

A 3x cut, and the task is exactly the shape Haiku is for: one email in,
one small fixed schema out, no tools, no reasoning.

Worth recording separately, because it is a real cost change nobody
decided: **Sonnet 5 is on introductory pricing of $2/$10 through
2026-08-31.** The $3/$15 above is what it reverts to. Extraction costs
rise ~50% on their own in five days.

## The measurement

397 labelled emails, replayed through `claude-haiku-4-5` via the
pipeline's own `extract_email`. 165s, 4 workers, ~$0.74.

```
agreed              385 / 397  (97.0%)
extraction failures 0          (tripwires 1 and 2 never fired)
signal rows         23         (meeting_request / needs_action / question)
MISSED SIGNAL       3          -> kept 20/23 (87%)
false alarms        5          (cheap)
other disagreement  4          (fyi/automated shuffling)
```

97% agreement, zero malformed output, a third of the price. On the
headline number this is an easy switch.

The headline number is the wrong number. Here are the three misses, all
`meeting_request` -> `automated`, described by role rather than by name
or address (ADR-030 — the argument needs the shape of each email, not the
parties to it):

1. **A leasing office's reply address** — "Come take a tour... Reply to
   this email or give us a call to schedule your personalized tour." No
   date, no time; Sonnet recorded `proposed_times: []`, `flexible: true`.
   **Haiku is arguably right here.** This is a bulk solicitation.
2. **A property-management platform's bulk tour-invite address**
   (`mail.<platform>.com`, sent via a marketing-automation vendor),
   subject "Group Tour Invite" — a named host, a specific unit, and
   **Tuesday, Aug 18, 2026 12:30 PM - 1 PM PDT**.
3. **The same sender** — a different named host, and **Saturday,
   Aug 15, 2026 11 AM - 11:30 AM PDT**.

(2) and (3) are not judgment calls. Each carries a named host, a unit
number, and an exact half-hour window, and Sonnet copied the time verbatim
into `proposed_times` exactly as the prompt asks. Haiku filed both as
newsletters. That the sender is a bulk marketing address is the point,
not an aside: it is ADR-016's finding restated — the signal arrives on
infrastructure that looks like noise.

## Decision

**Extraction stays on `claude-sonnet-5`.** Haiku 4.5 is rejected for this
stage on the measurement above.

**`eval_extraction.py` is kept** as the permanent harness, and
`extract_email` keeps its `model` parameter. The bake-off is now one
command, so the next candidate model is an afternoon's work rather than a
project.

## Why

Two dated, addressed, host-named tour invitations out of 23 signal rows
is a 9% loss on the one thing the product exists to catch. The saving is
$0.37 per 100 emails — at current volume roughly **$0.45 a day**.

That trade has been refused here before. ADR-016 turned down a 92% volume
cut for the same reason and in almost the same words: a filter that
removes the meeting requests does not make the secretary cheaper, it
makes it useless, and silently. The failure mode is identical — a
`meeting_request` relabelled `automated` never reaches the digest, and
nothing announces it. The only difference is that this time the cut was
41% smaller and the loss was 87% instead of 0%.

It is worth being precise about which errors matter, because the report
deliberately separates them. The 5 false alarms and 4 fyi/automated
shuffles are not arguments against Haiku: each costs at most one noisy
digest line. The asymmetry — cheap in one direction, unrecoverable in the
other — is the whole basis of the decision, and it is why the harness
prints `MISSED SIGNAL` on its own line rather than only an accuracy score.

**Downstream, the loss is worse than one digest line.** `proposals.py`
gates a calendar write on `proposed_times`, and ADR-024 makes that gate
deterministic. An email filed as `automated` never produces a proposal at
all, so the whole ADR-023 write path is dead for that email. The two
missed invitations are precisely the input that path was built for.

## What the number cannot tell you

- **The labels are not ground truth.** They are Sonnet's own output, so
  this measures agreement with the incumbent, not accuracy. That is the
  right question for a switch — a replacement must not lose what the
  incumbent caught — but a disagreement is not automatically the
  candidate's error. Miss (1) is the live example: reading it, Haiku
  looks more right than Sonnet.
- **23 signal rows is a small denominator.** 3/23 carries a wide
  interval, and a rerun could plausibly show 1 or 5.
- **The control was not run.** Sonnet scored against its own stored
  labels would separate model difference from run-to-run variance
  (`python eval_extraction.py --model claude-sonnet-5`, ~$2.20). It was
  skipped because it cannot change the decision: misses (2) and (3) are
  emails with the date printed in the body, and no plausible variance
  figure makes losing those acceptable for $0.45 a day.

## Rejected

- **Switch to Haiku 4.5 on the 97%.** The measurement above.
- **Haiku with a better prompt.** Probably closes some of the gap, and
  untested. Refused for now on sequencing, not on merit: it makes the
  prompt a variable at the same time as the model, and the pre-triage
  pass (still open) may remove most Haiku-eligible volume anyway. If it
  is tried, it needs its own run of this harness and its own ADR.
- **Haiku for `automated`-looking mail, Sonnet for the rest.** This is
  the pre-triage idea wearing a model-selection costume, and it needs the
  same thing pre-triage needs: a rule measured for recall on the 23
  signal rows first. That is a separate decision.
- **Batch API for the 50% discount.** Real money and genuinely applicable
  — the digest is twice-daily and could tolerate the turnaround. It is
  also a state machine (submit, poll, collect) bolted onto a pipeline
  whose current virtue is that it runs start to finish in one process.
  Not now; not never.

## Consequences

- Extraction cost is unchanged, and rises ~50% on 2026-08-31 when the
  Sonnet 5 introductory price ends. At ~120 emails/day that is roughly
  $0.67/day. **The cheap lever was never the model** — it is not calling
  the model at all for the 93% of mail that is `automated`, which is the
  pre-triage pass, still open.
- `extract_email` now takes `model` (default `MODEL`), and `THINKING_OFF`
  records which models need the explicit thinking off-switch. These are
  not per-model theatre: on Sonnet 5, omitting `thinking` runs **adaptive**
  thinking, so the switch is load-bearing; on Haiku 4.5 omitting it
  already means no thinking and `{"type": "disabled"}` is not part of its
  older thinking API. Sending one config to both would have been a 400 or
  a silently truncated JSON object.
- `evals/` joins `runs/` on the gitignore list. It holds real senders and
  subjects.
- The harness reads the store, so its statistical power grows every time
  the digest runs. Re-run it when the backlog drains — 23 signal rows is
  the weakest part of this ADR, and it is self-correcting.
