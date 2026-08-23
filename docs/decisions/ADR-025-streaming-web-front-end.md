# ADR-025: The front end is one static page served by the API, and /chat streams over SSE

Date: 2026-08-23
Status: accepted
Extends: ADR-020, ADR-017

## Context

ADR-020 built the loopback service and said every future front end would
be a client of its endpoints. `chat.py` proved that against a terminal.
Two problems were left explicitly open:

1. **`POST /chat` does not stream.** It returns when the graph finishes,
   which is often a minute. `chat.py` covers the gap with a spinner. A
   minute of a blank box is worse than a wrong answer, because you cannot
   tell a slow run from a hung one.
2. **The tool trace is in the wrong window.** `run_tools` prints
   `-> tool(args)` to the server's stdout. The one signal that explains
   the pause is visible only to whoever launched the process.

Both are the same missing thing: a response shape that carries events
during a run instead of one value after it.

A web page raised its own question — where does it live? Options were a
separate dev server (Vite/React on another port), a desktop shell, or
static files from the service itself.

## Decision

**One static HTML file, served by the API at `GET /`.** No build step,
no framework, no CDN dependency. `web/index.html` is a client of the HTTP
API, exactly as `chat.py` is; it is merely delivered by the same server.

**`POST /chat/stream`: Server-Sent Events, one JSON object per event.**

```
{"type": "thinking", "text": ...}    the model reasoning
{"type": "text",     "text": ...}    a piece of the answer
{"type": "tool",     "name": ..., "input": {...}}
{"type": "error",    "text": ...}    the run stopped early
{"type": "done",     "thread_id": ...}   always last
```

Underneath, `call_model` uses `client.messages.stream()` instead of
`.create()`, and both nodes emit through LangGraph's
`get_stream_writer()`. `stream_agent()` runs the graph with
`stream_mode="custom"`, so **only what the nodes deliberately emit leaves
the graph** — no state snapshots, therefore no email bodies escaping into
the event stream by accident.

`POST /chat` is unchanged and stays.

## Why

**Same origin is a security property, not a convenience.** ADR-020 has no
authentication because nothing off-loopback can reach the port. A page
served from a second dev server would need a CORS entry, and a CORS entry
is a standing invitation for one more origin to read your inbox through
this port. Serving `/` from the process that already holds the Gmail
token means the browser's own rules do the work: no CORS entry exists, so
no page anywhere else can call these endpoints.

**No build step, because the page is not the project.** This is a
learning project about LLM mechanics. A `node_modules` and a bundler add
a second toolchain, a second dependency tree to audit, and a compile step
between the code and what runs — for a chat box. When it outgrows one
file, that is a decision worth its own ADR.

**`stream_mode="custom"` rather than `"values"` or `"updates"`.** The
other modes stream graph STATE, and this graph's state is the whole
message history — untrusted email bodies included (ADR-004). Streaming
state to a client would mean every tool result crossing the wire on every
super-step. Custom mode inverts the default: nothing is visible unless a
node chose to emit it.

**The writer is a no-op outside a custom-mode run**, which is what let
this land without a branch in the node. `run_agent()` still calls
`GRAPH.invoke()` and still gets one string; `stream_agent()` calls
`GRAPH.stream()` and sees the deltas. One code path, two shapes.

**Text and thinking are separate event types.** A model that thinks, calls
a tool, thinks again, and only then answers produces both, interleaved.
Merging them would put reasoning into the answer. Splitting them lets a
client render one dimly and one plainly, or drop the reasoning entirely.

**Errors are events, not status codes.** By the time anything fails, a
`200` and half an answer have already been sent — the status line goes
out before the work starts and cannot be recalled. Every client of
`/chat/stream` must read `error` events; checking `response.ok` is not
enough. This is the real cost of streaming and it is worth stating
plainly.

## Rejected

**A React/Vite app on a second port.** Needs CORS on a service holding
Gmail credentials, needs a build step, needs a second process to run
before you can ask a question. Rejected for the reason ADR-020 gives for
having no auth: fewer reachable doors beats more guarded ones.

**Markdown rendering of replies.** `chat.py` already made this call and
was right: replies cite email ids and quote subjects, and a markdown
renderer eats the underscores and asterisks inside them. `white-space:
pre-wrap` shows what the model actually said. `/digest/latest` really is
markdown and can be rendered when the page grows a digest view.

**WebSockets.** Bidirectional, and this is not: the client sends one
message and then only listens. SSE is plain HTTP over the connection that
already exists, needs no new dependency, and dies cleanly when the tab
closes.

**`EventSource`.** The browser's built-in SSE client only speaks `GET`,
which would put the message in a query string and make the endpoint
inconsistent with `POST /chat`. Twelve lines of `fetch` + `ReadableStream`
parse the format by hand instead.

**Streaming tool ARGUMENTS as they arrive.** The API delivers them as
partial JSON deltas. Half-parsed arguments displayed to a human are worse
than a short wait; `run_tools` announces each call once, complete.

**Adding tool calls to `ChatResponse` instead** (the option `chat.py`'s
docstring names). It would fix the trace and not the waiting, and it
would still hand you everything at the end. Streaming fixes both.

## Consequences

- **A second chat endpoint to keep in step.** `/chat` and `/chat/stream`
  run the same graph on the same threads, but they are two routes, and a
  change to one is a change owed to the other.
- **`chat.py` still uses `/chat`**, so the console REPL keeps its spinner
  and still leaves the tool trace in the server window. It can move to
  `/chat/stream` whenever; nothing forces it.
- **Errors after the first byte are invisible to HTTP.** Anything reading
  `/chat/stream` and ignoring `error` events will silently show truncated
  answers as if they were complete.
- **Assistant messages are now dumped with `exclude_none=True`.**
  `.stream().get_final_message()` returns `ParsedTextBlock`, not
  `TextBlock`, and it carries an SDK-only `parsed_output` field. Dumped
  whole, that key reaches the checkpoint and the API rejects it on the
  NEXT turn — turn 1 is billed and succeeds, turn 2 dies on history it
  cannot edit, and the thread is broken permanently. Dropping nulls
  handles the class rather than the one field. The cost: a nullable field
  the API genuinely wants echoed back would be dropped silently. No
  current block type is like that.
- **Threads written before that fix stay broken.** Nothing repairs a
  checkpoint in place; `SqliteSaver.delete_thread` is the tool, and
  nothing calls it yet (ADR-022's open retention thread).
- **A third client of the graph now exists**, and it runs in a browser.
  The page never uses `innerHTML` — model output quotes untrusted subjects
  and bodies (ADR-004), and `innerHTML` would let a `<script>` in a
  sender's subject line execute inside the one page that can reach your
  inbox. `textContent` cannot do that. Any future edit to this page
  inherits that rule.
- **`web/` is committed** and holds no secrets — unlike `runs/` and
  `secretary.db`, it renders email content but never stores it.
- The service is now a web app in the ordinary sense. ADR-020's claim that
  it is "not a web app" is narrower than it was: `/` hands out one static
  file, and everything else is still JSON.
