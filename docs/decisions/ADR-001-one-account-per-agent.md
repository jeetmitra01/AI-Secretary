# ADR-001: One email account per agent instance

Date: 2026-07-27
Status: accepted

## Context
The secretary must eventually support both Gmail and Outlook. Two shapes
were possible: one agent that merges multiple accounts into a single
view, or one agent instance per account with the provider as a config
value (`EMAIL_PROVIDER=gmail|outlook`).

## Decision
One account per agent instance. Multiple accounts means multiple agent
instances. Provider selection is configuration, not logic.

## Why
- Single OAuth token per instance; no juggling parallel refresh flows.
- No cross-account deduplication (the same thread landing in two
  inboxes is a genuinely hard problem we simply skip).
- Unambiguous calendar authority in phase 2: one account, one calendar.
- Simpler state: one "last processed" watermark, not one per source.

## Rejected
Merged multi-account agent: every downstream component would carry
account-disambiguation logic, and dedup/threading across providers is
provider-specific pain with no learning payoff for this project.

## Consequences
- If a merged view is ever wanted, it becomes a *coordinator* that reads
  the outputs of several agent instances — not a rewrite of the agent.
- To keep that door open, `Email.source` is populated even though it is
  constant within any one instance (see ADR-002).
