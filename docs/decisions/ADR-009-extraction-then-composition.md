# ADR-009: Per-email extraction before digest composition

Date: 2026-07-27
Status: proposed (agreed in principle; built in phase 1)

## Context
At 20–75 emails/day, a digest's raw input is roughly 15–60k tokens —
inside the context window, but at the edge where single-pass
"dump everything in, ask for a summary" degrades: attention thins,
and hallucinations appear as subtle attribute errors (wrong date,
wrong sender, two threads merged) rather than invented emails.

## Decision
Two-stage pipeline:
1. Extraction — each email individually processed into structured
   JSON: {id, sender, intent, action_needed, meeting_request
   {who, proposed_times}, priority}.
2. Composition — the digest is written from those JSONs, grouped
   (needs action / meeting requests / FYI), citing email ids.

## Why
- Small focused calls hallucinate less than one giant call; errors
  are localized to a single email and testable in isolation.
- Structured intermediate output is verifiable code-side (does this
  id exist? is the date parseable?) before anything reaches the user.
- Id citations make every digest claim traceable to a source email.
- Stage 1 outputs are exactly what phase 2 meeting detection and the
  phase 3 research agent consume — the pipeline is the platform.

## Rejected
Single-pass digest over concatenated bodies: fewer API calls, but
error attribution is impossible and quality decays with volume.

## Consequences
- More API calls (one per email + one composition); at this volume,
  cost remains trivial with a mid-tier model.
- Requires an extraction schema kept deliberately small; every added
  field must earn its place in the digest or a downstream consumer.
- Bad extractions collected from real inbox runs become the
  regression test set.
