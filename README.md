# ExecAssistant

I want this to basically be an AI secretary

Design decisions and their reasoning live in [`docs/decisions/`](docs/decisions/).

## Running it

```bash
python run_digest.py        # one digest run: fetch -> extract -> compose
                            # -> archive to runs/ -> store -> toast
python server.py            # the local API on http://127.0.0.1:8765
python backfill.py          # one-shot: import existing runs/ into the store
python store.py             # what's in the database right now
python -m pytest tests -q   # tests
```

Twice-daily scheduling: see `SCHEDULING.md`. The scheduled task and the
API's `/ingest` call the same `run_digest.ingest()`, so they cannot drift.

## The pieces

| file | role |
| --- | --- |
| `connectors.py` | provider APIs -> the normalized `Email` (ADR-002) |
| `extraction.py` | one email -> one validated `Extraction` (ADR-009) |
| `composition.py` | extractions -> digest markdown; counts computed in code |
| `run_digest.py` | the pipeline + local delivery (ADR-014) |
| `store.py` | SQLite store of record: emails, extractions, runs (ADR-019) |
| `server.py` | loopback FastAPI service — the interaction surface (ADR-020) |
| `secretary_agent.py` | the hand-rolled agent loop (ADR-005) |
| `secretary_graph.py` | the same agent as a LangGraph graph (ADR-017) |
| `console.py` | utf-8 stdout; every entrypoint calls it first (ADR-021) |

## API

With `server.py` running, the generated console is at
<http://127.0.0.1:8765/docs>.

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

It binds to `127.0.0.1` and has no authentication, by design — it holds
Gmail credentials, so being unreachable from the network is a property of
the socket rather than of a check (ADR-020). `secretary.db` holds real
email bodies and is gitignored alongside `token.json` and `runs/`.
