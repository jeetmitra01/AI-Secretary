
# ADR-014: Local-first digest delivery (file + Windows toast)

Date: 2026-07-29
Status: accepted

## Context

The digest must reach the user twice daily. The original vision was
email-based delivery, but ADR-003 made v1 read-only — emailing the
digest requires adding the `gmail.send` scope. Options: local file +
console, Windows toast notification, or send-to-self email.

## Decision

Local-first: the full digest is written to a dated markdown file in
`runs/`, and a Windows toast shows code-computed headline counts,
opening the file on click. Toast failure never kills a run (console
and file always work). Delivery is isolated behind a single
`deliver()` function.

## Why

- Zero new OAuth scopes: ADR-003's capability restriction stays fully
  intact at the auth layer.
- Toasts are size-limited anyway; file + toast separates "notify"
  from "read" cleanly.
- Delivery-as-function means upgrading later is a swap, not a
  refactor.

## Rejected (for now)

Send-to-self email: viable with a design that preserves the security
model — the model never gets a send tool; deterministic code emails
composition output to a recipient hardcoded in config. Deferred, not
rejected on principle: local delivery is enough while accuracy is
being measured, and scope widening should be a deliberate step (its
own ADR) rather than a v1 convenience.

## Consequences

- Digests are only seen when at this machine; acceptable for the
  measurement phase.
- The eventual move to email delivery supersedes this ADR and must
  keep the "send capability exists in code, is never exposed to the
  model" property.
