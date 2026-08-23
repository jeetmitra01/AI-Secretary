# Architecture Decision Records

One short file per decision that shaped this system. Numbered in the
order decided. Statuses: `accepted` (in force), `proposed` (agreed in
principle, not yet built), `open` (question raised and framed, no
decision yet — the candidates live in the Decision section), `superseded
by ADR-NNN` (kept for history — never delete an ADR, mark it superseded
so the reasoning trail survives).

| #   | Decision                                            | Status   |
| --- | --------------------------------------------------- | -------- |
| 001 | One email account per agent instance                | accepted |
| 002 | Unified connector interface + normalized Email      | accepted |
| 003 | Read-only v1: security via capability restriction   | accepted |
| 004 | Email bodies are untrusted data                     | accepted |
| 005 | Hand-rolled agent loop before adopting LangGraph    | accepted |
| 006 | Overlapping fetch windows + dedupe by message id    | accepted |
| 007 | OAuth over app passwords / service accounts         | accepted |
| 008 | Platform timestamps over sender-supplied headers    | accepted |
| 009 | Per-email extraction before digest composition      | proposed |
| 010 | Trust MIME content over MIME labels                 | accepted |
| 011 | Ordering of fetch_since results                     | accepted |
| 012 | Surface fetch truncation instead of hiding it       | accepted |
| 014 | Local-first digest delivery: file + Windows toast   | accepted |
| 015 | Watermark advances only on a fully covered window   | accepted |
| 016 | No category filtering; marketing-subdomain denylist | accepted |
| 017 | LangGraph for orchestration only, not model access  | accepted |
| 018 | Add calendar.readonly on one scope-preserving token | accepted |
| 019 | SQLite is the store of record; runs/ is an archive   | accepted |
| 020 | Loopback HTTP service is the interaction surface     | accepted |
| 021 | Console encoding fixed in code, enforced by a test  | accepted |
| 022 | Durable chat threads: SqliteSaver in secretary.db   | accepted |
| 023 | Calendar writes behind a non-LLM executor zone      | accepted |
| 024 | Deterministic policy gate; auto-commit in shadow    | accepted |

## Template

```markdown
# ADR-NNN: <decision in one line>

Date: YYYY-MM-DD
Status: accepted | proposed | open | superseded by ADR-NNN

## Context
What situation forced a choice, and what options existed.

## Decision
What we chose, in one or two sentences.

## Why
The reasons that actually drove it.

## Rejected
Each alternative and the specific reason it lost.

## Consequences
What this commits us to, including the downsides we accepted.
```
