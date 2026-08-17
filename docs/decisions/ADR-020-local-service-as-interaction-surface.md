# ADR-020: A loopback HTTP service is the interaction surface; the digest becomes a client

Date: 2026-08-14
Status: accepted

## Context

The system is push-only: twice a day it decides what to tell you, and
between runs there is nothing to talk to. Every follow-up question —
"anything from the client about the contract?", "what's still
unanswered this week?" — has no surface to arrive on. ADR-017 built the
part that makes conversation possible (a checkpointed graph whose state
survives the function returning), and nothing consumes it.

The stated ambition was a GUI: a Chrome extension, or a chatbot. But no
UI can host what this agent needs — the Gmail token, the Anthropic
client, a Python connector stack. Whatever the front end is, it must
talk to a process that holds those. The GUI question is downstream of a
process question.

## Decision

Add `server.py`: a FastAPI app bound to **127.0.0.1 only**, fronting
the ADR-019 store and the ADR-017 graph. It exposes reads (digests,
emails, extractions, search), an ingest trigger, and a chat endpoint
carrying a `thread_id`.

`run_digest.py` keeps working standalone and unchanged in purpose. The
scheduled task stays exactly as SCHEDULING.md describes it. The digest
becomes one client of the store among several rather than the only way
information leaves the system.

## Why

- **It is the smallest change that makes every UI option cheap.** A web
  page, a Chrome extension, a Tauri shell, or a CLI all become thin
  clients of the same endpoints. Picking one stops being an
  architectural commitment.
- **ADR-014's delivery-as-a-function bet pays out here.** `deliver()`
  stays; the service is an additional consumer, not a rewrite of the
  pipeline.
- **ADR-017's checkpointer needs an owner.** A conversation that
  survives the function returning is worthless inside a process that
  exits after one digest. A long-lived server is what a `thread_id`
  means something in.
- **Loopback binding is a capability restriction, not a setting.** It
  is the same argument as ADR-003: the service fronts Gmail-authorized
  credentials, so it must be unreachable from the network by
  construction rather than by an auth check we remembered to write.

## Rejected

- **Chrome extension as the foundation.** Manifest V3 gives a service
  worker that is killed when idle, cannot hold OAuth or an API key
  safely, and cannot run Python at all — so it would end up calling a
  local server we would have to build anyway. Its genuine strength is
  overlaying Gmail's own UI (show the extraction beside the open
  thread), which is a *client* of this service and a much better second
  step than a first one.
- **Streamlit / Gradio as the app itself.** Fastest possible demo, but
  the UI framework becomes the process that owns the agent, and the
  Chrome-extension case then has nothing to call. Either is welcome as
  a *client* while the front end is still disposable.
- **Send-to-self email replies as the chat channel.** No new UI at all,
  but it needs `gmail.send` — precisely the scope ADR-003 and ADR-014
  refused — to solve a problem loopback HTTP already solves.
- **Exposing it on the LAN / adding auth.** Solves a problem nobody
  has: one user, one machine. Every remote-access story starts with
  authentication that does not exist yet, and loopback is the version
  that cannot be got wrong.

## Consequences

- There is now a process to keep running. Reads and chat work only
  while it is up; ingest and the digest keep working without it, which
  is the property that stops this from being a single point of failure.
- The `/ingest` endpoint can trigger the same pipeline the scheduler
  runs. Concurrent runs would double-fetch and race the watermark, so
  the endpoint must be serialized (single in-flight ingest).
- Chat inherits ADR-004 in a harder setting: the user's own words and
  untrusted email bodies now share one context window across turns.
  Delimiting stays mandatory, and read-only scope (ADR-003) remains the
  actual boundary — prompt wording is not the control.
- `InMemorySaver` becomes visibly wrong once the server restarts
  mid-conversation. Swapping in `SqliteSaver` against the ADR-019
  database is the follow-up, and ADR-017 already promised it costs one
  line and no node edits.
- CORS stays closed by default. A Chrome extension client will need an
  explicit origin allowance later; that is a deliberate change, not a
  wildcard now.
