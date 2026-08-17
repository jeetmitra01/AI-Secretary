# ADR-019: SQLite is the store of record; runs/ becomes an archive

Date: 2026-08-14
Status: accepted

## Context

Every run is a one-shot process (ADR-014): fetch, extract, compose,
write `runs/<timestamp>/{digest.md,extractions.json}`, exit. The only
memory between runs is `state.json` — a watermark and a list of seen
ids. Nothing can answer "what did that client say last Tuesday?"
without a human opening a folder.

Two things now need a queryable history rather than a pile of files:

1. An interactive surface (ADR-020) — a chat client's whole value is
   asking about mail the current run did not fetch.
2. Phase 3's RAG, which needs a corpus to index and a place to put
   embeddings next to the text they came from.

Note what `runs/` cannot supply even in principle: it stores
extractions but never the **email bodies** they were derived from.
Bodies today exist only inside a running process. Any question that
needs the original wording is currently unanswerable at any cost.

## Decision

Add `secretary.db` (SQLite) as the store of record: one row per
email — body included — one row per extraction, one row per run with
its digest markdown. Ingestion writes there before the watermark
advances.

`runs/` keeps being written unchanged. It is demoted from "the data"
to "the immutable per-run archive and eval dataset".

## Why

- **SQLite is already a dependency of the plan.** `langgraph`'s durable
  checkpointer (`SqliteSaver`, ADR-017's one-line swap) is SQLite. One
  file, no server, no daemon to keep alive — the same operational
  weight as the JSON files it replaces.
- **Provider message ids are a natural primary key** (ADR-002 already
  guarantees they are stable and unique per account). That makes
  re-ingesting an email idempotent — `INSERT OR REPLACE` — which is
  exactly the property ADR-006 wants when it says duplicates are cheap
  and gaps are catastrophic. Overlap re-fetch stays free.
- **Bodies become durable.** ADR-004 does not change: a stored body is
  still untrusted data, and every path that puts one in a prompt still
  delimits it. Storage is not trust.
- **FTS5 ships with Python's stdlib sqlite3** (verified on this
  machine), so keyword search over subjects and bodies costs no new
  dependency and gives the chat agent a real `search_emails` tool.

## Rejected

- **Keep `runs/*.json` and glob it.** Works for tens of runs and dies
  at hundreds; no search, no join from extraction back to its email,
  and — decisively — no bodies. It is a log, not a store.
- **Postgres.** The right answer for a multi-user service and the wrong
  one for a single-user local agent: a server process to install, run,
  and back up, in exchange for concurrency this system will never have.
  ADR-020 keeps everything on loopback; one writer is the whole design.
- **A vector store now (Chroma/LanceDB) as the primary store.** Phase 3
  needs embeddings, but embeddings are a derived index, not the source
  of truth. Storing the canonical text in SQLite leaves the choice of
  vector store fully open, and re-indexing from rows is cheap.
- **Encrypting the database (SQLCipher).** Deferred, not dismissed. The
  db holds the same plaintext already sitting in `runs/` and the same
  account `token.json` grants, so it does not widen exposure today; the
  honest fix is disk encryption, not an app-level layer that would need
  a key this machine would then store next to the data.

## Consequences

- The db becomes a real asset with real content. It is machine-local,
  untracked, and belongs in `.gitignore` next to `token.json` — a
  committed `secretary.db` leaks an inbox.
- Writes happen before `save_state()`. A failed write therefore leaves
  the watermark un-advanced and the window gets re-covered next run,
  which the primary key absorbs. That ordering is load-bearing.
- WAL mode is required, not optional: a scheduled ingest writing while
  the ADR-020 service reads would otherwise raise "database is locked".
- Schema changes need migrations from here on. With one file and one
  user this is a small tax, but it is no longer zero.
- Extraction failures are stored as rows too (an `error` column rather
  than a separate table), so extraction.py's tripwire 3 — failures are
  visible, never silently dropped — survives into history.
