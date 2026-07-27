# ADR-003: v1 is read-only — security via capability restriction

Date: 2026-07-27
Status: accepted

## Context
The agent ingests untrusted third-party text (email) by design, and
LLM instruction-following cannot be made injection-proof by prompting
(see ADR-004). A hallucinating or injected model with send/write
powers can cause real damage: emails sent as the user, meetings booked
from misread dates.

## Decision
v1 has no ability to act on the world: `gmail.readonly` scope, no
sending, no calendar writes. The agent reports and recommends; the
human acts. Autonomy arrives in phases: drafts folder only (phase 4),
auto-send/calendar writes only after weeks of observed accuracy.

## Why
Defense ranking, most to least effective:
1. Capability restriction — an injected agent with no dangerous tools
   has nothing to steal or do. Enforced at the OAuth scope level, so
   even buggy code cannot exceed it.
2. Blast-radius limits — drafts not sends, read-only calendar.
3. Prompt hardening — helpful, never sufficient (probabilistic).
4. Human in the loop for anything irreversible.
Security investment runs from architecture down to prompting — the
reverse of where prompt-first intuition puts it.

## Rejected
Autonomous scheduling/sending from day one: the cost of one confident
misreading ("not Tuesday" booked as Tuesday) exceeds the convenience,
and trust should be earned against observed accuracy, not assumed.

## Consequences
- Widening an OAuth scope is a security decision, not a config tweak;
  it requires deleting the cached token and re-authorizing, and should
  get its own ADR when it happens.
- Some conveniences are deliberately impossible in v1; friction here
  is the control working as intended.
