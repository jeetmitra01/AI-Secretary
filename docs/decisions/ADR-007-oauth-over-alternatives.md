# ADR-007: OAuth over app passwords and service accounts

Date: 2026-07-27
Status: accepted

## Context
The connector needs mailbox read access. Live options: app password +
IMAP, service account with domain-wide delegation, OAuth authorization
flow. (Raw basic auth is dead — both providers disabled it; API keys
identify applications, not users, and Gmail does not accept them.)

## Decision
OAuth (installed-app flow) with the `gmail.readonly` scope. Browser
consent happens once; the refresh token cached in `token.json` is
silently exchanged for short-lived access tokens thereafter.

## Why
- Only option that can express *scoped* access — the read-only
  restriction of ADR-003 is literally inexpressible in an app
  password, which grants full read/delete/send.
- Independently revocable from Google account settings without a
  password change.
- Mandatory front door to the Gmail and Calendar APIs needed for
  phases 1–2; IMAP has no calendar equivalent.

## Rejected
- App password + IMAP: fastest to first fetch, but all-or-nothing
  power, raw MIME parsing, no calendar path. Right for a weekend
  throwaway; wrong for a system that grows.
- Service account + domain-wide delegation: the eventual model if
  this becomes a product sold to companies, but requires Workspace
  (unavailable on personal Gmail) and is wildly over-powered for one
  user — a leaked key is every mailbox in a domain.

## Consequences
- `credentials.json`, `token.json`, and `.env` are secrets:
  gitignored, never committed.
- Scope changes require deleting `token.json` and re-consenting —
  useful friction that makes privilege escalation deliberate.
- Consent screen stays in Google's "Testing" mode (self as test
  user), avoiding the verification process for personal use.
