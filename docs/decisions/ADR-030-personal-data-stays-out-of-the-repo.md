# ADR-030: Personal data stays out of the repository

Date: 2026-08-26
Status: accepted

## Context

`.gitignore` has always held the obvious things — `token.json`,
`credentials.json`, `secretary.db`, `runs/`, `.env`. The rule they follow
was never written down, so it was applied to files and not to content,
and content leaked past it.

Two findings prompted this.

**1. `eval_extraction.py` wrote one file containing everything.** Its 397
rows each carry a real sender name and address, a subject line, and a
Gmail message id:

```json
{"id": "...", "sender": "<a named agent at a leasing company>",
 "subject": "<a street address, unit number, and city>", ...}
```

Individually harmless. As a set, the 397 subject lines disclose which
apartments were toured and when, which jobs were applied to and which
progressed, which bank and credit-bureau accounts exist, and a moving
company with a quote reference — all timestamped to a few weeks. That is
a location, employment, and financial profile of one person, plus the
names and addresses of third parties who never agreed to appear in a
dataset.

**2. The same class of data was already committed.** `ADR-016` named a
recruiter and two leasing offices; `ADR-029` named two tour hosts and a
unit number; `connectors.py` carries the owner's own address inside a
VERP example. `evals/` was never the boundary — `docs/` had crossed it
first, and nothing flagged that.

Nothing is exposed today: the GitHub remote is private. This ADR exists
so that stays true by construction rather than by luck, and so "make the
repo public" is a decision that can actually be evaluated.

## Decision

**Real personal data does not enter tracked files.** Three mechanisms,
because a rule with no enforcement is the thing that failed already.

**1. `eval_extraction.py` writes two files.** `*-summary.json` is counts
and a confusion matrix, and is the only eval output git tracks.
`*-rows.json` holds the per-email detail and is ignored.

`build_summary()` **assembles the summary from aggregates**. It is not a
row with the identifying keys deleted, and it must not become one — a
strip-the-bad-keys function ships the next key somebody adds to
`replay()`, silently and by default, whereas counting cannot leak a field
it was never told about. `tests/test_eval_redaction.py` asserts both the
absence of identifiers and the fixed key set.

**2. `evals/` is deny-by-default in `.gitignore`:**

```gitignore
evals/*
!evals/*-summary.json
```

An output file added later is ignored until somebody deliberately
un-ignores it. (`evals/*`, not `evals/` — git cannot re-include a file
inside an excluded *directory*, so the negation would be dead with the
shorter form. A test covers this too: correct redaction plus a leaky
ignore file is still a leak.)

**3. ADRs describe people by role.** "A leasing office, on a `knck.io`
relay" carries the whole argument; the name on the other end never did.

### The line, precisely

Not every address is personal data, and pretending otherwise would break
working code.

| Keep verbatim | Anonymize |
| --- | --- |
| Corporate broadcast addresses (`promos@email.guitarcenter.com`) | Named individuals (`firstname.lastname@company`) |
| Platform relays identical for every user (`hit-reply@linkedin.com`) | Any address showing who *this* mailbox personally dealt with |
| Fabricated examples (`attacker@x.com` in ADR-004) | Subjects with street addresses, unit numbers, reference numbers |
| Sender values that are **live code** — `BULK_SENDERS` in `connectors.py` has to match the real string to function | Correspondent names, in prose or data |

The test is not "is this an email address" but **"does this reveal a
person, or what one person did"**. `promos@email.guitarcenter.com` reveals
neither. `a-leasing-office@example.invalid` in a list of "kept 6/6" reveals which
leasing office this mailbox was negotiating with.

## Why

**Because the interesting half of the eval is the half that cannot be
published.** ADR-029 was decided by *reading three misses* — the senders
and subjects are what made them legible. Redacting them destroys exactly
the evidence that mattered. There is no clever framing that avoids this
trade; the resolution is that the console still prints everything (it is
not a committed artefact) and only the file split is enforced.

**Because git is not a filesystem.** `git rm` leaves the blob in history.
Removing it needs a rewrite and a force push, forks keep their copies,
and public repositories are scraped for addresses within hours. The cost
of the first mistaken commit is not the file — it is a history rewrite,
or living with it.

**Because the third-party harm is not the owner's to accept.** The owner
can publish their own apartment search. The leasing agents and recruiters
in that data cannot consent on the owner's behalf, and they gain nothing
from the disclosure.

**Because the deny-by-default direction is the same one ADR-023 already
chose.** There, capability is withheld and granted narrowly. Here,
tracking is withheld and granted narrowly. Both are safe when someone
forgets, which is the only property that matters in a rule about
forgetting.

## Rejected

- **Commit the full eval output; the repo is private.** Private today.
  This ADR exists precisely so that going public later is a decision
  rather than an accident.
- **Redact senders and subjects in the rows file too.** Then the misses
  cannot be read, and ADR-029 could not have been written. The local file
  keeps everything; only what is *tracked* is restricted.
- **Hash the identifiers.** Looks rigorous and is not: a 397-row set with
  hashed senders still discloses the subjects, and subjects are where the
  street addresses are. Hashing a small, guessable domain is reversible
  anyway.
- **Rewrite git history now to purge the committed ADR names.** Deferred,
  not refused. The remote is private, so the exposure is zero and the
  rewrite is disruptive. **It becomes required before the repository is
  made public** — anonymizing the working tree does not remove the old
  blobs from history.
- **A pre-commit hook scanning for address patterns.** Attractive, and it
  would have caught ADR-016. Not adopted yet because the useful version
  needs the keep/anonymize distinction above, and a regex cannot make it —
  it would flag `BULK_SENDERS` on every commit and be disabled within a
  week. Revisit if the manual rule slips.

## Consequences

- `evals/*-summary.json` is committable and is a genuinely useful
  artefact: the confusion matrix is richer than the headline counts and
  names nobody. The 2026-08-26 Haiku run is the first one.
- The eval's console output is unchanged and still prints senders and
  subjects. That is deliberate — the operator has to read the misses.
- `ADR-016` and `ADR-029` now describe people by role. The measurements,
  the counts, and the arguments are untouched; only the identifications
  are gone. `ADR-004` needed no change: its `attacker@x.com` is invented.
- **The working tree is clean; history is not.** `git log -p` still
  contains the earlier ADR-016 text. That is accepted while the remote is
  private, and is the blocking item if that ever changes.
- `connectors.py` still contains the owner's own address in the ADR-010
  VERP example. Left alone deliberately: it is the owner's own data, and
  the example is load-bearing — it is the evidence for why `-from:me` was
  rejected. Worth revisiting in the same pass as a history rewrite.
- Anything added to `evals/` that is not a `*-summary.json` is invisible
  to git. If a future output is meant to be tracked, the negation has to
  be widened on purpose, which is the point.
