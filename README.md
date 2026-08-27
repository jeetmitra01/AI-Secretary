# ExecAssistant

I want this to basically be an AI secretary

Design decisions and their reasoning live in [`docs/decisions/`](docs/decisions/).

## Setup

Python 3.10 or newer (developed on 3.12). You need a Google account whose
mail this reads, and a key for each of the two model providers.

**1. Install the dependencies**

```bash
python -m pip install -r requirements.txt
```

**2. Provider keys**

Extraction defaults to an OpenAI model (ADR-031), while the agent loop and
the graph call Anthropic — both are required, not alternatives. Each SDK
reads its own key from the environment, so put both in a `.env` beside
this file:

```
ANTHROPIC_API_KEY=...
OPENAI_API_KEY=...
```

**3. A Google OAuth client of your own**

There is no shared client; you create one:

1. In the Google Cloud console, make a project and enable the **Gmail
   API** and the **Google Calendar API**.
2. On the OAuth consent screen, choose **External** and add your own
   address as a test user.
3. Create an **OAuth client ID** of type **Desktop app**, download the
   JSON, and save it here as `credentials.json`.

**4. Consent**

```bash
python auth.py           # opens a browser; grants every scope below
python auth.py --check   # report what is granted now; opens no browser
```

That writes **two** token files, deliberately (ADR-027):

| file | scopes | held by |
| --- | --- | --- |
| `token.json` | `gmail.readonly`, `calendar.readonly` | the digest and the agent |
| `token_write.json` | `calendar.events` | `executor.py`, and nothing else |

The code that puts untrusted email into a model's context never holds a
scope that can change anything (ADR-003, ADR-023). That split is the
security boundary rather than any prompt wording, so the files must not be
merged.

**5. First run**

```bash
python run_digest.py     # creates secretary.db and state.json as it goes
python server.py         # then open http://127.0.0.1:8765/
```

`.env`, `credentials.json`, both token files, `secretary.db` and `runs/`
are all gitignored, and hold either credentials or real mail (ADR-030).
Delivery ends in a Windows toast; on other platforms that step is skipped
and the run still completes.

## Running it

```bash
python run_digest.py        # one digest run: fetch -> extract -> compose
                            # -> archive to runs/ -> store -> toast
python server.py            # the local API + chat page on
                            # http://127.0.0.1:8765
python chat.py              # console REPL against that API
python backfill.py          # one-shot: import existing runs/ into the store
python auth.py              # consent to every scope the service needs
python auth.py --check      # what is granted now; opens no browser
python store.py             # what's in the database right now
python executor.py          # pending calendar proposals; <id> commits one
python executor.py --consent  # one-off: mint the calendar write token
python -m pytest tests -q   # tests
```

Twice-daily scheduling: see `SCHEDULING.md`. The scheduled task and the
API's `/ingest` call the same `run_digest.ingest()`, so they cannot drift.

## The pieces

| file | role |
| --- | --- |
| `auth.py` | Google OAuth + the grant registry; `python auth.py` consents (ADR-027) |
| `connectors.py` | provider APIs -> the normalized `Email` (ADR-002) |
| `extraction.py` | one email -> one validated `Extraction` (ADR-009) |
| `composition.py` | extractions -> digest markdown; counts computed in code |
| `run_digest.py` | the pipeline + local delivery (ADR-014) |
| `store.py` | SQLite store of record: emails, extractions, runs, proposals (ADR-019) |
| `calendars.py` | read-only calendar behind a connector interface (ADR-018) |
| `proposals.py` | the typed handoff between zones + the policy gate (ADR-024) |
| `executor.py` | the ONLY calendar write path; not a tool the model can call (ADR-023) |
| `server.py` | loopback FastAPI service — the interaction surface (ADR-020) |
| `chat.py` | console REPL; talks HTTP only, imports none of the system |
| `web/index.html` | the chat page: one static file, no build step (ADR-025) |
| `secretary_agent.py` | the hand-rolled agent loop (ADR-005) |
| `secretary_graph.py` | the same agent as a LangGraph graph (ADR-017) |
| `console.py` | utf-8 stdout; every entrypoint calls it first (ADR-021) |

## API

With `server.py` running, the chat page is at <http://127.0.0.1:8765/>
and the generated API console at <http://127.0.0.1:8765/docs>.

| endpoint | what it gives you |
| --- | --- |
| `GET /health` | row counts, latest run, whether an ingest is in flight |
| `GET /digest/latest` | the newest digest as markdown |
| `GET /runs`, `/runs/{id}` | run history with coverage numbers |
| `GET /emails` | newest first; filter by `intent`, `priority`, `days` |
| `GET /emails/{id}` | one email, full body |
| `GET /threads/{id}` | a conversation in order |
| `GET /search?q=` | keyword search over subject, body, sender |
| `POST /ingest` | run the pipeline now (one at a time; 409 if busy) |
| `POST /chat` | ask the agent; `thread_id` continues a conversation |
| `POST /chat/stream` | the same run as SSE: text, thinking and tool events |
| `GET /proposals` | calendar events the agent queued for your approval |
| `POST /proposals/{id}/confirm` | commit one — the only write path (ADR-023) |
| `POST /proposals/{id}/reject` | decline one; the row is kept, not deleted |
| `GET /` | the chat page, served same-origin (ADR-025) |

`/chat/stream` sends one JSON object per SSE event — `text`, `thinking`,
`tool`, `error`, then `done`. Errors arrive as `error` events rather than
as status codes: the `200` is already sent by the time anything can fail,
so a client that only checks `response.ok` will show truncated answers as
if they were complete (ADR-025).

## Calendar writes: two zones, and only one of them can write

The agent reads untrusted email, so it does not get a tool that changes
your calendar. It gets `propose_calendar_event`, which writes a row to the
`proposals` table and nothing else. `executor.py` holds the write scope,
on its own token, and takes a proposal ID rather than a sentence — so an
instruction hidden inside an email body ends its journey as a pending row
that you read (ADR-023).

```
agent (reads email)  ->  proposals table  ->  executor.py (writes)
   no write tool          typed payload        no email, no prose
```

The executor is plain Python, not a second LLM: with attendees and
deletion out of scope there is no judgement left to make, only validation
and a policy check.

Whether a proposal could ever commit unattended is decided by
deterministic checks — is the sender known, is the slot free, is the time
inside sane bounds — not by the model's stated confidence, which the email
body itself can influence (ADR-024). Auto-commit ships **off**;
`python executor.py` shows the verdicts it would have reached, which is
the evidence for turning it on later.

Everything created is `tentative`, on your own calendar, with no
attendees, so nothing this system does can send mail. Proposals expire
after 48 hours.

```bash
python executor.py --consent   # one-off: mint token_write.json
python executor.py             # what is waiting, and why
```

In the REPL: `/proposals`, `/confirm <id>`, `/reject <id> [reason]`.

It binds to `127.0.0.1` and has no authentication, by design — it holds
Gmail credentials, so being unreachable from the network is a property of
the socket rather than of a check (ADR-020). `secretary.db` holds real
email bodies and is gitignored alongside `token.json` and `runs/`.
