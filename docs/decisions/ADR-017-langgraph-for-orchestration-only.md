# ADR-017: Adopt LangGraph for orchestration only, not for model access

Date: 2026-08-07
Status: accepted

## Context

ADR-005 deferred LangGraph until the loop it abstracts had been written
and debugged by hand, and predicted the migration would be "mechanical".
That precondition is met: `secretary_agent.py` is the hand-rolled loop
running against the real Gmail connector.

Phase 3 (research agent + RAG) is where the hand-rolled loop stops being
adequate. It needs branching that isn't a single `if`, multi-step plans
that can be resumed, and conversation that survives the function
returning — the last of which the bare loop cannot do at all, because its
entire state is a local variable.

The choice was not whether to adopt LangGraph, but how much of the
LangChain ecosystem comes with it.

## Decision

Install `langgraph` alone. Nodes call `client.messages.create()`
directly, and graph state holds **raw Anthropic message dicts**.

Explicitly not adopted: `langchain-anthropic` / `ChatAnthropic`,
`create_react_agent`, LangChain tool decorators, and LangChain
`BaseMessage`. The tool schemas stay as the Anthropic-shaped dicts they
already were.

## Why

- ADR-005's reason survives adoption. The framework was deferred so the
  loop wouldn't be magic; routing model access through `ChatAnthropic`
  would re-hide exactly what was deliberately learned — `tool_use`
  blocks, `tool_result` arriving as a **user** message, and the
  resend-everything-every-turn cost that makes context growth visible.
- Because state is raw Anthropic dicts, the reducer is `operator.add`
  rather than langgraph's `add_messages`. That helper understands
  LangChain messages, and we have none. Fighting it would be the first
  sign of adopting the wrong layer.
- Fewer moving parts, and no pin to LangChain's model abstraction. This
  project already has a provider seam — `EmailConnector` (ADR-002). It
  does not need a second one at the model layer.

## Evidence the migration was mechanical, as ADR-005 predicted

`secretary_graph.py` imports `TOOLS`, `TOOL_FUNCTIONS`, `MODEL`,
`MAX_ITERATIONS` and the client from `secretary_agent.py` and
reimplements none of them. The whole port is a state schema, two nodes,
and three edges. Had the tools needed rewriting, the abstraction would
have been leaking.

What the framework replaced, all of it bookkeeping:

    for _ in range(MAX_ITERATIONS)   ->  a step budget (recursion_limit)
    if stop_reason != "tool_use"     ->  a conditional edge
    messages.append(...)             ->  a state reducer

What it added, measured offline with the API stubbed:

    conditional cycle model->tools->model            2 API turns
    context growth per turn                          [1, 3] messages
    checkpoint retains history across invoke()       4 -> 6 messages
    follow-up resends full prior thread              5 messages
    separate thread_id starts clean                  2 messages

The follow-up case is the one the bare loop had no answer for: it began
from an empty list on every call.

## Rejected

- **Full LangChain stack** (`ChatAnthropic` + `create_react_agent`).
  Less code and what every tutorial shows, but it hides the message
  plumbing this project exists to understand, and a prebuilt ReAct agent
  would make the loop opaque again one phase after it was made clear.
- **Stay hand-rolled through phase 3.** Viable for the digest, which is
  a straight pipeline, but the research agent needs resumable multi-step
  state, and hand-rolling a checkpointer is not the lesson.
- **LangChain tool decorators.** They would require rewriting schemas
  that already work, purely to satisfy a second format.

## Consequences

- `langchain-core` is installed anyway, as a transitive dependency of
  `langgraph`. It is not imported by our code; that it is present is not
  permission to start using it.
- `recursion_limit` counts **super-steps** (node executions), not model
  turns. One round trip is two steps, so the leash is set to
  `MAX_ITERATIONS * 2`. Reading it as "turns" is the classic cause of an
  agent that stops early.
- `InMemorySaver` lasts only for the process. Swapping in a durable saver
  is a one-line change with no edit to any node — which is the argument
  for the checkpointer abstraction in the first place.
- `secretary_agent.py` stays. It is ADR-005's teaching artifact and the
  reference the port is diffed against; deleting it would discard the
  evidence that the migration was mechanical.
- **A wart the port inherited and did not fix:** the dedupe cache in
  `secretary_agent.fetch_recent_emails` is a module-global `_seen_ids`
  set. It is therefore *not* part of graph state, so it is neither
  checkpointed nor isolated per thread — two thread_ids share one cache,
  and the second conversation to ask for email sees the first one's
  leftovers. Tool state that belongs in graph state is the first thing to
  fix before the research agent multiplies the number of threads.
