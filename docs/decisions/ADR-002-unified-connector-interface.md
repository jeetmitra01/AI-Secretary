# ADR-002: Unified connector interface + normalized Email object

Date: 2026-07-27
Status: accepted

## Context
Gmail (Google API) and Outlook (Microsoft Graph) differ in auth,
message format, body encoding (base64url MIME tree vs HTML), threading
semantics, and timestamp conventions. Agent logic that touches provider
APIs directly would be written twice and tested twice.

## Decision
All provider access goes through an `EmailConnector` abstract interface
(`fetch_since`, later `fetch_thread` / `create_draft`). Every connector
returns the same frozen `Email` dataclass. Nothing downstream of a
connector may import or reference a provider SDK.

The Email contract:
- `body_text` is always plain text — connectors strip HTML.
- `received_at` is always timezone-aware UTC.
- `source` ("gmail"/"outlook") is always populated, even though it is
  constant per instance (per ADR-001), so records from multiple
  instances can be merged later without guessing.

## Why
- The agent, summarizer, and meeting extractor are written once.
- Provider quirks are quarantined where they can be contract-tested
  ("no duplicates on overlapping fetch", "bodies are plain text",
  "timestamps are UTC") instead of leaking into prompts.
- Agents are only as good as the boring data layer beneath them;
  normalization is where provider chaos is converted into invariants
  the LLM pipeline can rely on.

## Rejected
Direct API usage per feature: duplicated logic, provider details in
prompts, and any second provider doubles the surface area forever.

## Consequences
- Adding Outlook is one class implementing the interface plus passing
  the shared contract tests — zero changes downstream.
- The interface must stay minimal; convenience methods that only one
  provider can support cheaply are a smell.
