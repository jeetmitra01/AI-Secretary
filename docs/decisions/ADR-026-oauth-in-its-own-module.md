# ADR-026: Google OAuth moves to auth.py; scope constants stay with their callers

Date: 2026-08-23
Status: accepted; the no-__main__ decision is superseded by ADR-027
Amends: ADR-002, ADR-007

## Context

`load_credentials`, `_renew` and `ReauthorizationRequired` lived in
`connectors.py`, next to the Gmail connector. That was right when Gmail
was the only Google capability. It is not right any more — there are now
three callers, and two of them are not mail:

| caller | scope | token file |
| --- | --- | --- |
| `connectors.py` | `gmail.readonly` | `token.json` |
| `calendars.py` | `calendar.readonly` | `token.json` |
| `executor.py` | `calendar.events` | `token_write.json` |

So `calendars.py` and `executor.py` each imported an exception and a
credential loader **from the mail module**, which says nothing true about
either. Worse, `executor.py` is the ADR-023 write zone, whose whole
premise is that it does not touch mail — and its import line said
otherwise.

## Decision

`auth.py` holds `ReauthorizationRequired`, `load_credentials` and
`_renew`, and nothing else. Every caller imports from there. The code
inside is unchanged: this is a move, not a rewrite.

**Scope constants do NOT move.** `GMAIL_SCOPES` stays in
`connectors.py`, `CALENDAR_SCOPES` in `calendars.py`,
`CALENDAR_WRITE_SCOPES` in `executor.py`.

**`auth.py` has no `__main__`.** There is no `python auth.py`. Re-consent
still runs through the entrypoint that needs the scope.

> **Superseded by ADR-027.** `python auth.py` now consents to every
> grant, as two flows on two token files. The scope-placement
> decision above stands and is now enforced by tests.

## Why

**The dance is a property of Google OAuth, not of mail.** The 7-day
Testing expiry, the scope-sufficiency check that `creds.valid` does not
do, the never-open-a-browser-unattended rule — none of them mention
Gmail. A file that three unrelated capabilities import should not be
named after one of them.

**Scopes stay put because a scope is a capability claim.** ADR-003 rests
on the grant being narrow, and the way that stays true is that widening
one is a visible edit next to the code that uses it. A shared
`ALL_SCOPES` list in `auth.py` would read like a menu, and "while I'm in
here" is how a read-only project stops being read-only. Keeping them
apart also keeps the ADR-023 split honest: `calendar.events` is declared
in the executor, which is the only thing allowed to hold it.

**No `__main__`, because consent should be proved, not just granted.**
`python calendars.py` re-consents and then prints today's busy blocks —
so the grant is exercised in the same breath. A generic `python auth.py`
would mint a token and tell you nothing about whether the capability it
bought actually works.

## Rejected

**Leave it in `connectors.py` and accept the odd import.** Cheapest, and
it keeps a false statement in the executor's import line — the one file
where "this does not touch mail" is a security property (ADR-023).

**Re-export from `connectors.py` for compatibility.** Two valid import
paths for one exception, forever, and the wrong one keeps working. Every
importer was updated instead: `calendars.py`, `executor.py`,
`run_digest.py`, `server.py`, `tests/test_proposals.py`.

**Move the scope constants too**, giving one file that answers "what can
this project do?". Tempting for review, but it puts the write scope and
the read scopes in one list, and it moves the decision away from the code
the decision is about. Rejected on ADR-003 grounds.

**A `GoogleAuth` class holding credentials_file/token_file as state.**
The callers already pass both explicitly, and two of them deliberately
pass DIFFERENT token files. State would hide exactly the distinction
ADR-023 depends on.

## Consequences

- `from connectors import ReauthorizationRequired` no longer works. It is
  `from auth import ReauthorizationRequired`.
- `connectors.py` drops to importing one name, `load_credentials`, and
  lost its now-unused `sys` and `Path` imports.
- `auth.py` is not an entrypoint, so the ADR-021 `use_utf8()` test does
  not apply to it. Its two `print` calls go to stderr and carry scope
  URLs and Google error text. That is the one path in the project that
  prints without a guaranteed guard — every process that can reach it is
  an entrypoint that already called `use_utf8()`.
- The re-consent message in `_renew` still names `python connectors.py`
  for every failure, including a missing CALENDAR scope, where
  `python calendars.py` is the correct command. Pre-existing, unchanged
  by this move, and now easier to fix in one place.
