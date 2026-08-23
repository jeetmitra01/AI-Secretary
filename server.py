"""
ADR-020: the loopback service. The interaction surface the digest never
had.

    uvicorn server:app --reload        (dev)
    python server.py                   (same thing, bound explicitly)

Then: http://127.0.0.1:8765/docs — FastAPI generates the API console, so
there is something clickable before any front end exists.

What this is NOT: a web app. It holds the Gmail token, the Anthropic
client and the connector stack, and it binds to 127.0.0.1 so that being
unreachable from the network is a property of the socket rather than of
an auth check we remembered to write (the ADR-003 argument, one layer
down). Every future front end — a page, a Chrome extension overlaying
Gmail, a desktop shell — is a client of these endpoints.

Reads come from the ADR-019 store. Ingest calls the exact function the
scheduler calls. Chat runs the ADR-017 graph, which is what a
checkpointer was built for and what a process that exits after one
digest could never use.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Literal

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

BASE = Path(__file__).parent
load_dotenv(BASE / ".env")          # before anything constructs Anthropic()

import store                                            # noqa: E402
import run_digest                                       # noqa: E402
from connectors import ReauthorizationRequired          # noqa: E402
from console import use_utf8                            # noqa: E402
# Light by design (stdlib + pydantic), so importing the read API does not
# drag in either zone's clients. executor.py, which holds the write
# capability, is imported lazily inside the two endpoints that need it.
from proposals import CalendarProposal                  # noqa: E402

HOST = "127.0.0.1"                  # ADR-020. Not a config knob: making this
PORT = 8765                         # 0.0.0.0 exposes an authenticated inbox
                                    # to the network, and nothing in this
                                    # service authenticates anybody.

app = FastAPI(
    title="Secretary",
    description="Local-only API over the email store and the agent.",
    version="0.1.0",
)

# No CORS middleware on purpose (ADR-020): a same-origin page served from
# here needs none, and a wildcard would let any website in your browser
# read your inbox through this port. The Chrome-extension client will
# need one explicit origin added here — a deliberate edit, not a default.


# --- plumbing --------------------------------------------------------------

def db() -> Iterator[sqlite3.Connection]:
    """One connection per request, closed on the way out.

    A single shared connection would be simpler and wrong: FastAPI runs
    sync endpoints in a threadpool, and a sqlite3 connection belongs to
    the thread that made it. Per-request is the version that cannot
    surprise us — WAL (store.connect) is what makes it cheap.
    """
    conn = store.connect()
    try:
        yield conn
    finally:
        conn.close()


# One ingest at a time, process-wide. Two concurrent runs would double-fetch
# the same window and race each other on state.json's watermark — and the
# loser would advance it past mail the winner never stored. The scheduled
# task is a separate PROCESS and this lock cannot see it; that overlap is
# absorbed by dedupe (ADR-006), not by this.
_ingest_lock = threading.Lock()


# --- response shapes -------------------------------------------------------

class RunSummary(BaseModel):
    id: str
    started_at: str
    total_matching: int
    fetched: int
    missed: int
    ok_count: int
    failed_count: int


class IngestResult(BaseModel):
    run_id: str
    fetched: int
    total_matching: int
    missed: int
    truncated: bool
    ok_count: int
    failed_count: int
    digest: str


class ProposalSummary(BaseModel):
    id: str
    created_at: str
    expires_at: str
    status: str
    source_email_id: str | None = None
    # The rendered line, not the raw payload: it puts the verbatim phrase
    # from the email next to the resolved instant, which is the whole
    # mechanism for catching a timezone misread (ADR-023).
    reads_as: str
    policy_ok: bool | None = None
    policy_failed: list[str] = Field(default_factory=list)
    event_id: str | None = None


class DecisionResult(BaseModel):
    proposal_id: str
    status: str
    message: str
    event_id: str | None = None
    policy_ok: bool | None = None
    policy_failed: list[str] = Field(default_factory=list)
    policy_summary: str | None = None


class RejectRequest(BaseModel):
    reason: str = ""


class ChatRequest(BaseModel):
    message: str
    thread_id: str = Field(
        default="default",
        description="Same id continues the conversation (ADR-017). A new "
                    "id starts clean.")


class ChatResponse(BaseModel):
    thread_id: str
    reply: str


# --- status ----------------------------------------------------------------

@app.get("/health")
def health(conn=Depends(db)) -> dict:
    return {
        "ok": True,
        "db": str(store.DB_PATH),
        "counts": store.counts(conn),
        "ingest_running": _ingest_lock.locked(),
        "latest_run": (store.latest_run(conn) or {}).get("id"),
    }


# --- digests ---------------------------------------------------------------

@app.get("/runs", response_model=list[RunSummary])
def runs(limit: int = Query(30, ge=1, le=200), conn=Depends(db)):
    return store.list_runs(conn, limit=limit)


@app.get("/digest/latest", response_class=PlainTextResponse)
def latest_digest(conn=Depends(db)) -> str:
    """The markdown, as markdown. This endpoint is the twice-daily file,
    fetchable — the smallest possible replacement for opening runs/."""
    run = store.latest_run(conn)
    if not run or not run["digest_md"]:
        raise HTTPException(404, "no digest yet — run an ingest")
    return run["digest_md"]


@app.get("/runs/{run_id}")
def run_detail(run_id: str, conn=Depends(db)) -> dict:
    run = store.get_run(conn, run_id)
    if not run:
        raise HTTPException(404, f"no run {run_id!r}")
    return run


# --- email reads -----------------------------------------------------------

@app.get("/emails")
def emails(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    intent: Literal["meeting_request", "needs_action", "question",
                    "fyi", "automated"] | None = None,
    priority: Literal["high", "medium", "low"] | None = None,
    days: int | None = Query(None, ge=1, le=365,
                             description="only mail from the last N days"),
    conn=Depends(db),
) -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)) if days else None
    return store.list_emails(conn, limit=limit, offset=offset, intent=intent,
                             priority=priority, since=since)


@app.get("/emails/{email_id}")
def email_detail(email_id: str, conn=Depends(db)) -> dict:
    """The one endpoint that returns a FULL body — store.get_email does no
    snippetting, while the list and search views do. That asymmetry is the
    context-budget rule from secretary_agent.fetch_recent_emails, kept at
    the API layer: a caller pays for a whole body only by asking for one."""
    email = store.get_email(conn, email_id)
    if not email:
        raise HTTPException(404, f"no email {email_id!r}")
    return email


@app.get("/threads/{thread_id}")
def thread(thread_id: str, conn=Depends(db)) -> list[dict]:
    found = store.get_thread(conn, thread_id)
    if not found:
        raise HTTPException(404, f"no thread {thread_id!r}")
    return found


@app.get("/search")
def search(q: str = Query(..., min_length=1),
           limit: int = Query(20, ge=1, le=100),
           conn=Depends(db)) -> list[dict]:
    """Keyword search (FTS5). Free text is safe here — store._fts_query
    quotes every token, so a question mark is a non-match, not a 500."""
    return store.search_emails(conn, q, limit=limit)


# --- ingest ----------------------------------------------------------------

@app.post("/ingest", response_model=IngestResult)
def ingest() -> IngestResult:
    """Run the pipeline now — the same `run_digest.ingest()` the scheduled
    task runs, so triggering from a UI and triggering from Task Scheduler
    cannot drift apart.

    Deliberately not async: this blocks for as long as extraction takes
    (seconds per email). As a sync endpoint FastAPI runs it in the
    threadpool, so reads keep being served while it works.
    """
    if not _ingest_lock.acquire(blocking=False):
        raise HTTPException(409, "an ingest is already running")
    try:
        result = run_digest.ingest()
    except ReauthorizationRequired as e:
        # 503, not 500: the service is fine, the Gmail grant is not, and
        # only a human at a terminal can fix it (`python connectors.py`).
        raise HTTPException(503, f"gmail reauthorization required: {e}")
    finally:
        _ingest_lock.release()

    return IngestResult(
        run_id=result.run_id,
        fetched=result.coverage.fetched,
        total_matching=result.coverage.total_matching,
        missed=result.coverage.missed,
        truncated=result.coverage.truncated,
        ok_count=len(result.extractions),
        failed_count=len(result.failures),
        digest=result.digest,
    )


# --- proposals (ADR-023) ---------------------------------------------------
#
# The human half of the two-zone split. The agent can only ever produce a
# pending row; these three endpoints are the only way one becomes a real
# calendar event, and they are reachable only from 127.0.0.1 (ADR-020).

def _summarize(row: dict) -> ProposalSummary:
    proposal = CalendarProposal.model_validate_json(row["payload"])
    return ProposalSummary(
        id=row["id"], created_at=row["created_at"],
        expires_at=row["expires_at"], status=row["status"],
        source_email_id=row["source_email_id"],
        reads_as=proposal.human(),
        policy_ok=None if row["policy_ok"] is None else bool(row["policy_ok"]),
        policy_failed=json.loads(row["policy_failed"] or "[]"),
        event_id=row["event_id"])


@app.get("/proposals", response_model=list[ProposalSummary])
def proposals(status: str = Query("pending",
                                  description="A status, or 'all'."),
              limit: int = Query(20, ge=1, le=200), conn=Depends(db)):
    """Pending calendar proposals, newest first.

    The TTL sweep runs on read rather than on a schedule: a scheduled job
    that quietly stops running would leave week-old proposals looking
    confirmable, and this endpoint is the thing that would show them.
    """
    store.expire_proposals(conn)
    rows = store.list_proposals(conn, None if status == "all" else status,
                                limit=limit)
    return [_summarize(r) for r in rows]


@app.post("/proposals/{proposal_id}/confirm", response_model=DecisionResult)
def confirm_proposal(proposal_id: str, conn=Depends(db)) -> DecisionResult:
    """Commit one proposal — the only write path in the system.

    `executor` is imported lazily for the same reason `secretary_graph` is:
    it is the module holding the Google write client, and a read endpoint
    should keep working while it is mid-edit or its dependencies are
    missing.

    The executor re-runs the policy gate itself before it calls the API, so
    nothing here needs to check anything (ADR-024).
    """
    import executor

    result = executor.commit(conn, proposal_id, actor="human")
    if result["status"] == "unknown":
        raise HTTPException(404, f"no proposal {proposal_id}")
    if result["needs_consent"]:
        # 503 like ingest: the service is fine, the write grant is not, and
        # only a human at a terminal can fix it.
        raise HTTPException(503, result["message"])
    return DecisionResult(**{k: v for k, v in result.items()
                             if k != "needs_consent"})


@app.post("/proposals/{proposal_id}/reject", response_model=DecisionResult)
def reject_proposal(proposal_id: str, req: RejectRequest,
                    conn=Depends(db)) -> DecisionResult:
    """Decline a proposal. Kept as a row, not deleted — shadow mode needs
    the rejections as much as the confirmations (ADR-024)."""
    import executor

    result = executor.reject(conn, proposal_id, req.reason)
    if result["status"] == "unknown":
        raise HTTPException(404, f"no proposal {proposal_id}")
    return DecisionResult(**{k: v for k, v in result.items()
                             if k != "needs_consent"})


# --- chat ------------------------------------------------------------------

@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    """Ask the agent something. `thread_id` continues a conversation.

    Imported lazily so the read endpoints stay usable even if the graph
    or its dependencies are broken — the store is the durable part, the
    agent is the part most likely to be mid-edit.

    Threads are durable (ADR-022): the checkpointer writes into the same
    secretary.db the reads above come from, so a `thread_id` from before a
    restart — or from yesterday — still continues.

    The caveat that remains: ADR-004 is harder here than in the digest.
    Your words and untrusted email bodies share one context window across
    many turns, and now across many days. Read-only scope (ADR-003) is the
    actual boundary.
    """
    from secretary_graph import run_agent

    reply = run_agent(req.message, thread_id=req.thread_id)
    return ChatResponse(thread_id=req.thread_id, reply=reply)


# --- entrypoint ------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    use_utf8()          # uvicorn logs request paths and tracebacks, both of
                        # which can carry non-ASCII (console.py)

    print(f"Secretary API on http://{HOST}:{PORT}  (docs at /docs)")
    uvicorn.run(app, host=HOST, port=PORT)
