# ADR-027: `python auth.py` consents to everything, as two grants on two token files

Date: 2026-08-24
Status: accepted
Amends: ADR-026

## Context

ADR-026 moved OAuth into `auth.py` and deliberately gave it no
`__main__`. The argument was that re-consent should run through the
entrypoint that needs the scope, because that entrypoint proves the grant
works: `python calendars.py` consents AND prints today's busy blocks.

In practice that argument lost to a simpler one. Re-consent is needed
every 7 days (the Testing-mode refresh expiry), and it means remembering
three commands and which one covers what. Worse, the failure message in
`_renew` names `python connectors.py` for every failure, including a
missing CALENDAR scope — so following the instruction verbatim did not
fix the problem it reported.

The obvious fix is one command that asks for everything. The obvious
IMPLEMENTATION of it is a security hole.

## Decision

`python auth.py` consents to every scope the service needs. It runs
**one OAuth flow per token file**, and there are two:

| grant | token file | scopes |
| --- | --- | --- |
| read | `token.json` | `gmail.readonly`, `calendar.readonly` |
| write | `token_write.json` | `calendar.events` |

`python auth.py --check` reports what is granted now and opens no
browser. A grant that is already satisfied is skipped, so re-running
costs nothing.

`auth.GRANTS()` is the registry and the extension point. It **references**
the scope constants where they live (ADR-026) rather than restating them.

Four tests in `tests/test_auth_grants.py` enforce the split: no
non-`.readonly` scope may sit on `token.json`, the write scopes must live
on a different file, and no file may carry both kinds.

The per-capability entrypoints stay. `python connectors.py`,
`python calendars.py` and `python executor.py` still work and are still
what PROVES a grant, which is why each `Grant` carries a `proves` field
naming its command.

## Why

**One token would have destroyed the only real defence.** The request was
"ask for all the permissions the service needs". Read literally, that is
one flow, one token, every scope. `token.json` is held by the scheduled
digest and by `/chat` — the two paths that put untrusted email bodies
into a model's context (ADR-004). Putting `calendar.events` there makes a
write capability reachable by anyone who can send you mail, and nothing
downstream would fail, log, or look different. ADR-003 says security comes
from the capability restriction; this is that restriction. The convenience
was worth having; the merge was not, so the command mints two tokens.

**Two browser windows are the feature, not a wart.** Each flow is one
token file. Watching consent happen twice is the separation being
enforced, rather than described in a comment.

**A test, because the next capability is one line.** Email drafts
(`gmail.compose`) are next. The easiest line to write is the one that
appends to the tuple already there, and it would be silently catastrophic.
`.readonly` as the tripwire uses Google's own naming convention:
`gmail.compose`, `gmail.modify` and `calendar.events` all fail it.

**Scope constants still do not move.** ADR-026's reasoning survives
intact — a scope is a claim about what one component may do, and it
belongs beside that component. `GRANTS()` decides only which TOKEN each
scope lands on, which is a different question and the one this file is
entitled to answer.

**`--check` exists because "what do I have right now?" had no answer.**
Previously you learned your token was short a scope by running something
that failed. Reporting must never refresh, consent, or raise, so
`granted_scopes()` reads the file directly instead of going through
`load_credentials`.

## Rejected

**One flow, one token, all scopes.** The literal reading of the request.
Rejected above — it is ADR-023 deleted.

**Keep ADR-026's no-`__main__` rule.** Its reasoning was sound and lost to
weekly friction. The concern it protected — that minting a token proves
nothing — is kept by the `proves` field and printed after every run.

**A single `ALL_SCOPES` list.** Rejected in ADR-026 and still rejected:
it moves the decision away from the code the decision is about, and it
reads like a menu.

**Deriving the grants by importing every module and scraping `*_SCOPES`.**
Automatic, and it would silently pick up a new write scope and file it
wherever the module suggested. The whole value here is that a human wrote
down which token a capability lands on.

**Making `auth.py` run the smoke tests itself.** It would have to import
the connector, the calendar and the executor and call live APIs — turning
the auth module into a thing that reads mail. It prints the commands
instead.

## Consequences

- ADR-026's "no `__main__`" is superseded. Its scope-placement decision
  is not, and is now enforced by tests rather than by prose.
- `ReauthorizationRequired` now names `python auth.py`, which is correct
  for every scope. The wrong-command bug ADR-026 flagged is fixed by
  this, not worked around.
- `auth.py` is an entrypoint, so ADR-021 applies: it calls `use_utf8()`,
  and the existing parametrized test now covers it (56 tests -> 57).
- `GRANTS()` imports `connectors`, `calendars` and `executor` lazily,
  because all three import `auth` at module level. Module-level imports
  here would be a cycle.
- Adding a capability now touches two places: the scope constant beside
  its code, and one line in `GRANTS()`. The docstring on `GRANTS()` spells
  out the rule for choosing the token file.
- The registry is a single place that lists everything this service can
  do to your Google account. That is useful for review and it is also a
  map for an attacker who gets the repo. The repo holds no credentials,
  so this trades nothing that was secret.
