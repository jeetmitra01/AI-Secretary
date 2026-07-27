# ADR-005: Hand-rolled agent loop before adopting LangGraph

Date: 2026-07-27
Status: accepted

## Context
This is a learning project: the goal is understanding LLM behaviour
(context windows, tool use, hallucination, agent control flow), not
just shipping. Frameworks like LangGraph abstract exactly the
machinery being studied.

## Decision
Build the raw agent loop first (~100 lines against the Anthropic SDK:
send messages + tool schemas, branch on `stop_reason == "tool_use"`,
execute, append `tool_result` blocks in a user message, repeat with a
`MAX_ITERATIONS` leash). Adopt LangGraph in phase 3, once the loop it
abstracts has been written and debugged by hand.

## Why
- The loop teaches the non-obvious facts directly: the model executes
  nothing (every "action" is local code honoring a structured
  request); the API is stateless, so context growth is visible per
  iteration; tool results travel as *user* messages, which is the
  structural root of prompt injection (ADR-004).
- After writing it by hand, LangGraph reads as "my loop plus state
  management, persistence, and branching" instead of magic.

## Rejected
Framework-first: faster to a demo, but leaves tool calling, context
management, and failure modes as black boxes — the opposite of the
project's purpose.

## Consequences
- Phase 1–2 code carries some boilerplate a framework would remove;
  that is accepted tuition.
- When LangGraph lands in phase 3, migration is mechanical: tools and
  the Email layer are framework-agnostic already.
