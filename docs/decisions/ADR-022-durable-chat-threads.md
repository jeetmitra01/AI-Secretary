# ADR-022: Chat threads are durable — SqliteSaver in the ADR-019 database

Date: 2026-08-17
Status: accepted

## Context

ADR-017 adopted LangGraph for the checkpointer: the thing the hand-rolled
loop had no answer for. It shipped with `InMemorySaver`, which keeps
checkpoints in the process that made them. So `POST /chat` continued a
conversation only until the server restarted, and `python
secretary_graph.py` started from nothing every time.

That makes the interaction surface much less useful than it looks. A
follow-up question is the whole reason `thread_id` exists, and "restart
the server" is a normal event — an edit, a reboot, a dead token.

`secretary.db` already exists as the store of record (ADR-019), already
runs in WAL mode, and is already gitignored as sensitive.

## Decision

`SqliteSaver` over one long-lived `sqlite3` connection to
`secretary.db`, opened with `check_same_thread=False`, with `setup()`
called at import. LangGraph adds two tables of its own, `checkpoints` and
`writes`.

## Why

- One database file. One thing to back up, one WAL to reason about, and
  a conversation and the emails it discusses are queryable side by side —
  which Phase 3's RAG will want.
- `check_same_thread=False` is the flag `SqliteSaver`'s own documentation
  prescribes, because the saver holds a `threading.Lock` and serializes
  access itself.
- `setup()` at import surfaces a bad path or a read-only directory while
  the module loads, instead of inside somebody's first chat request.

## The apparent contradiction with ADR-019

`store.py` says a `sqlite3` connection belongs to the thread that made
it, and opens one per request. This does the opposite. Both are correct,
and the reason is worth keeping:

- A **store** connection is short-lived and touched by exactly one
  thread. Nothing is shared, so nothing needs a lock.
- A **checkpointer** connection is owned by the compiled graph and
  outlives every request. It *cannot* be per-request, so the library
  serializes access with a lock instead.

The rule underneath both: a connection may cross threads only when
something guarantees one thread touches it at a time. `store.py`
guarantees that by not sharing; `SqliteSaver` guarantees it with a lock.

## Rejected

- **A separate `checkpoints.sqlite`.** Cleaner separation on paper. In
  practice it is a second file to back up, a second WAL, and it makes
  "which conversation discussed this email" a cross-database join for no
  gain. The table names do not collide.
- **`SqliteSaver.from_conn_string()`.** The obvious-looking API, and
  wrong here: it is a context manager that closes the connection on
  exit, while the graph is module-level and outlives any `with` block.
- **A checkpointer per request.** Matches `store.py`'s shape, but the
  checkpointer is an argument to `compile()`, so this means recompiling
  the graph per request — paying setup cost per call to imitate a rule
  whose reason (no cross-thread sharing) the lock already satisfies.
- **Keeping `InMemorySaver`, adding a thread cache.** Rebuilding a
  durable store badly, in front of a database that is already open.

## Consequences

- Checkpoints hold serialized conversation state, so untrusted email
  bodies are now on disk in a second place. ADR-004 is unchanged by
  that: storage is not trust, and every path back into a prompt still
  delimits them. `secretary.db` was already gitignored for this reason.
- Threads accumulate forever. `SqliteSaver` exposes `delete_thread` and
  `prune`, and nothing calls them yet. Growth is one row per super-step,
  each holding a full message history, so a chatty thread is not small.
  When it matters, retention is its own decision.
- A `thread_id` is now a durable, guessable name for a stored
  conversation. The service is loopback-only (ADR-020), so that is not
  an access-control problem today; it would become one the moment
  anything else can reach the port.
