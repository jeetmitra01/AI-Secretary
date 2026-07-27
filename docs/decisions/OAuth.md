
# ADR-003: OAuth over app passwords

Date: 2026-07-27
Status: accepted

## Context

Needed mailbox read access. Options: app password + IMAP,
service account, OAuth.

## Decision

OAuth with gmail.readonly scope.

## Why

Only option supporting scoped access — read-only is our primary
security control. Also revocable without a password change, and
required for the Calendar API in phase 2.

## Rejected

App passwords: all-or-nothing access, no read-only variant, no
calendar path. Service accounts: Workspace-only, over-powered.
