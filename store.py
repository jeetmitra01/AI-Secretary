"""
ADR-019: SQLite is the store of record.

Three tables and one search index, in the shape the rest of the system
already thinks in:

    emails       one row per normalized Email (models.Email), body kept
    extractions  one row per extraction attempt, success OR failure
    runs         one row per digest run: coverage numbers + the markdown

`runs/` on disk keeps being written by run_digest.py. It is the
immutable per-run archive; this file is what you query.

Everything here is plain sqlite3 — no ORM. The schema is small enough
that SQL is the clearest description of it, and Phase 3's RAG will want
to write its own queries against these rows anyway.

Bodies live here now (they used to exist only inside a running process).
ADR-004 is unchanged by that: a stored body is still untrusted
third-party data, and every path that puts one in a prompt still
delimits it. Storage is not trust.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from models import Email

BASE = Path(__file__).parent
DB_PATH = BASE / "secretary.db"


# --- schema ----------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS emails (
    id           TEXT PRIMARY KEY,   -- provider message id (ADR-002: stable,
                                     -- unique per account) — a natural key, so
                                     -- re-ingesting the same mail is a no-op
    thread_id    TEXT NOT NULL,
    sender       TEXT NOT NULL,
    recipients   TEXT NOT NULL,      -- JSON array; we never query INTO it
    subject      TEXT NOT NULL,
    body_text    TEXT NOT NULL,      -- plain text always (ADR-002/010)
    received_at  TEXT,               -- ISO-8601 UTC, always +00:00 so that
                                     -- lexicographic order == chronological
    source       TEXT NOT NULL,      -- "gmail" | "outlook"
    first_run_id TEXT                -- run that first ingested it
);
CREATE INDEX IF NOT EXISTS emails_received_at ON emails(received_at DESC);
CREATE INDEX IF NOT EXISTS emails_thread      ON emails(thread_id);

CREATE TABLE IF NOT EXISTS extractions (
    email_id      TEXT PRIMARY KEY, -- deliberately NOT a foreign key: every
                                    -- run before ADR-019 produced extractions
                                    -- whose bodies were never stored, and
                                    -- backfill.py imports them. An orphan
                                    -- extraction is real history, not corrupt
                                    -- data; it simply doesn't join to a body
                                    -- until that email is fetched again.
    run_id        TEXT NOT NULL,
    intent        TEXT,              -- NULL on failure
    summary       TEXT,
    action_needed TEXT,
    meeting       TEXT,              -- JSON MeetingRequest, NULL if none
    priority      TEXT,
    error         TEXT,              -- NULL = success. Failures are ROWS, not
                                     -- omissions: extraction.py's tripwire 3
                                     -- ("visible, never silently dropped")
                                     -- has to survive into history too.
    raw           TEXT NOT NULL      -- model_dump_json, verbatim
);
CREATE INDEX IF NOT EXISTS extractions_run    ON extractions(run_id);
CREATE INDEX IF NOT EXISTS extractions_intent ON extractions(intent);

CREATE TABLE IF NOT EXISTS runs (
    id             TEXT PRIMARY KEY, -- "2026-08-14-1430", same as runs/ dir
    started_at     TEXT NOT NULL,
    total_matching INTEGER NOT NULL, -- Coverage, straight from the connector
    fetched        INTEGER NOT NULL,
    missed         INTEGER NOT NULL,
    ok_count       INTEGER NOT NULL,
    failed_count   INTEGER NOT NULL,
    digest_md      TEXT
);

-- External-content FTS: the index stores no copy of the text, it points at
-- emails.rowid. Keeps the db roughly one copy of your inbox instead of two.
CREATE VIRTUAL TABLE IF NOT EXISTS emails_fts USING fts5(
    subject, body_text, sender,
    content='emails', content_rowid='rowid'
);
"""

# The triggers are what keep an external-content index honest — it does not
# observe writes to `emails` on its own.
#
# Note there is no INSERT-OR-REPLACE path anywhere in this file, and that is
# deliberate: REPLACE deletes the old row, but its DELETE trigger only fires
# when `recursive_triggers` is on. Relying on that would leave the search
# index quietly stale after every overlap re-fetch. `ON CONFLICT DO UPDATE`
# fires the update trigger unconditionally, so upsert is the safe idiom here.
TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS emails_fts_ins AFTER INSERT ON emails BEGIN
    INSERT INTO emails_fts(rowid, subject, body_text, sender)
    VALUES (new.rowid, new.subject, new.body_text, new.sender);
END;
CREATE TRIGGER IF NOT EXISTS emails_fts_del AFTER DELETE ON emails BEGIN
    INSERT INTO emails_fts(emails_fts, rowid, subject, body_text, sender)
    VALUES ('delete', old.rowid, old.subject, old.body_text, old.sender);
END;
CREATE TRIGGER IF NOT EXISTS emails_fts_upd AFTER UPDATE ON emails BEGIN
    INSERT INTO emails_fts(emails_fts, rowid, subject, body_text, sender)
    VALUES ('delete', old.rowid, old.subject, old.body_text, old.sender);
    INSERT INTO emails_fts(rowid, subject, body_text, sender)
    VALUES (new.rowid, new.subject, new.body_text, new.sender);
END;
"""


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    """Open (creating if needed) and return a ready connection.

    `path=None` rather than `path=DB_PATH` in the signature: a default
    argument is evaluated once at import, so the DB_PATH spelled there
    would be frozen and no test or alternate profile could ever redirect
    it by setting store.DB_PATH. Resolving inside the body keeps the
    module-level constant the single source of truth.

    WAL is not a tuning knob here, it is a correctness requirement
    (ADR-019): a scheduled ingest writes while the server reads, and the
    default rollback journal makes readers and the writer block each
    other into "database is locked".
    """
    conn = sqlite3.connect(path or DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    conn.executescript(TRIGGERS)
    return conn


# --- writes ----------------------------------------------------------------

def _iso_utc(dt: datetime | None) -> str | None:
    """One timestamp format in the column, or the index sorts wrong."""
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat()


def save_emails(conn: sqlite3.Connection, emails: list[Email],
                run_id: str) -> int:
    """Upsert emails. Idempotent by primary key, which is what makes
    ADR-006's overlapping window free: re-covering a window costs a few
    redundant writes, never a duplicate row.

    first_run_id is preserved on conflict — it records when we FIRST saw
    the mail, and a re-fetch is not a second sighting.
    """
    rows = [(e.id, e.thread_id, e.sender, json.dumps(e.recipients),
             e.subject, e.body_text, _iso_utc(e.received_at), e.source, run_id)
            for e in emails]
    conn.executemany(
        """INSERT INTO emails (id, thread_id, sender, recipients, subject,
                               body_text, received_at, source, first_run_id)
           VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
               thread_id=excluded.thread_id, sender=excluded.sender,
               recipients=excluded.recipients, subject=excluded.subject,
               body_text=excluded.body_text,
               received_at=excluded.received_at, source=excluded.source""",
        rows)
    conn.commit()
    return len(rows)


def save_extractions(conn: sqlite3.Connection, extractions: list,
                     failures: list, run_id: str) -> int:
    """Both outcomes into one table. A later run that succeeds where an
    earlier one failed overwrites the failure row — the current state of
    an email is one extraction, and runs/ keeps the per-run history."""
    rows = []
    for e in extractions:
        rows.append((e.id, run_id, e.intent, e.summary, e.action_needed,
                     e.meeting.model_dump_json() if e.meeting else None,
                     e.priority, None, e.model_dump_json()))
    for f in failures:
        rows.append((f.id, run_id, None, None, None, None, None,
                     f.error, f.model_dump_json()))

    conn.executemany(
        """INSERT INTO extractions (email_id, run_id, intent, summary,
                                    action_needed, meeting, priority,
                                    error, raw)
           VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT(email_id) DO UPDATE SET
               run_id=excluded.run_id, intent=excluded.intent,
               summary=excluded.summary, action_needed=excluded.action_needed,
               meeting=excluded.meeting, priority=excluded.priority,
               error=excluded.error, raw=excluded.raw""",
        rows)
    conn.commit()
    return len(rows)


def save_run(conn: sqlite3.Connection, run_id: str, coverage, digest: str,
             ok_count: int, failed_count: int,
             started_at: datetime | None = None) -> None:
    conn.execute(
        """INSERT INTO runs (id, started_at, total_matching, fetched, missed,
                             ok_count, failed_count, digest_md)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
               total_matching=excluded.total_matching,
               fetched=excluded.fetched, missed=excluded.missed,
               ok_count=excluded.ok_count, failed_count=excluded.failed_count,
               digest_md=excluded.digest_md""",
        (run_id, _iso_utc(started_at or datetime.now(timezone.utc)),
         coverage.total_matching, coverage.fetched, coverage.missed,
         ok_count, failed_count, digest))
    conn.commit()


# --- reads -----------------------------------------------------------------

def latest_run(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        "SELECT * FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def list_runs(conn: sqlite3.Connection, limit: int = 30) -> list[dict]:
    rows = conn.execute(
        """SELECT id, started_at, total_matching, fetched, missed,
                  ok_count, failed_count
           FROM runs ORDER BY started_at DESC LIMIT ?""", (limit,)).fetchall()
    return [dict(r) for r in rows]


def get_run(conn: sqlite3.Connection, run_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    return dict(row) if row else None


def get_email(conn: sqlite3.Connection, email_id: str) -> dict | None:
    row = conn.execute(
        """SELECT e.*, x.intent, x.summary, x.action_needed, x.meeting,
                  x.priority, x.error
           FROM emails e LEFT JOIN extractions x ON x.email_id = e.id
           WHERE e.id = ?""", (email_id,)).fetchone()
    return _email_row(row) if row else None


def get_thread(conn: sqlite3.Connection, thread_id: str) -> list[dict]:
    rows = conn.execute(
        """SELECT e.*, x.intent, x.summary, x.action_needed, x.meeting,
                  x.priority, x.error
           FROM emails e LEFT JOIN extractions x ON x.email_id = e.id
           WHERE e.thread_id = ? ORDER BY e.received_at ASC""",
        (thread_id,)).fetchall()
    return [_email_row(r) for r in rows]


def list_emails(conn: sqlite3.Connection, limit: int = 50, offset: int = 0,
                intent: str | None = None, priority: str | None = None,
                since: datetime | None = None,
                body_chars: int | None = 400) -> list[dict]:
    """Newest first, optionally filtered. LEFT JOIN, not INNER: an email
    whose extraction failed still exists and must still be listable."""
    where, params = ["1=1"], []
    if intent:
        where.append("x.intent = ?"); params.append(intent)
    if priority:
        where.append("x.priority = ?"); params.append(priority)
    if since:
        where.append("e.received_at >= ?"); params.append(_iso_utc(since))

    rows = conn.execute(
        f"""SELECT e.*, x.intent, x.summary, x.action_needed, x.meeting,
                   x.priority, x.error
            FROM emails e LEFT JOIN extractions x ON x.email_id = e.id
            WHERE {' AND '.join(where)}
            ORDER BY e.received_at DESC LIMIT ? OFFSET ?""",
        (*params, limit, offset)).fetchall()
    return [_email_row(r, body_chars) for r in rows]


def search_emails(conn: sqlite3.Connection, query: str, limit: int = 20,
                  body_chars: int | None = 400) -> list[dict]:
    """Keyword search over subject/body/sender, best match first.

    Ranked by bm25 with subject weighted above body: a hit in a subject
    line is a much stronger signal than one buried in a footer.
    """
    match = _fts_query(query)
    if not match:
        return []
    rows = conn.execute(
        """SELECT e.*, x.intent, x.summary, x.action_needed, x.meeting,
                  x.priority, x.error
           FROM emails_fts f
           JOIN emails e ON e.rowid = f.rowid
           LEFT JOIN extractions x ON x.email_id = e.id
           WHERE emails_fts MATCH ?
           ORDER BY bm25(emails_fts, 10.0, 1.0, 5.0)
           LIMIT ?""", (match, limit)).fetchall()
    return [_email_row(r, body_chars) for r in rows]


def counts(conn: sqlite3.Connection) -> dict:
    """Cheap dashboard numbers, computed in SQL — never by a model. Same
    rule composition.stats_line follows: counts come from the data."""
    row = conn.execute(
        """SELECT (SELECT COUNT(*) FROM emails)                      AS emails,
                  (SELECT COUNT(*) FROM extractions WHERE error IS NULL)
                                                                AS extractions,
                  (SELECT COUNT(*) FROM extractions
                    WHERE error IS NOT NULL)                        AS failures,
                  (SELECT COUNT(*) FROM runs)                       AS runs
        """).fetchone()
    return dict(row)


# --- helpers ---------------------------------------------------------------

_FTS_TOKEN = re.compile(r"[A-Za-z0-9_@.\-]+")


def _fts_query(raw: str) -> str:
    """Turn free text into a safe FTS5 MATCH expression.

    FTS5 has its own query syntax, so a natural-language question
    ("what's the deal with Q3?") is a SYNTAX ERROR, not a poor search —
    and this input arrives from a chat agent, i.e. from a model that has
    been reading untrusted email. Quoting every token means the query
    language can never be reached from the outside at all: the worst a
    hostile string can do is match the wrong emails.

    OR, not AND. AND is the tempting default and it makes the tool
    useless the first time a model passes a whole question instead of
    keywords — one stopword absent from the corpus and a perfectly good
    query returns nothing. OR keeps recall, and bm25 does the work AND
    was doing: common words carry almost no weight, so the documents
    matching the RARE terms sort to the top anyway. Ranking beats
    filtering when the caller can't be trusted to phrase a query.
    """
    tokens = _FTS_TOKEN.findall(raw or "")
    return " OR ".join(f'"{t}"' for t in tokens)


def _email_row(row: sqlite3.Row, body_chars: int | None = None) -> dict:
    """One row -> the JSON shape clients and tools see.

    body_chars is context budgeting, the same lesson as
    secretary_agent.fetch_recent_emails: list views hand a model dozens
    of emails at once, so they get a snippet, and only get_email returns
    the whole body.
    """
    d = dict(row)
    d["recipients"] = json.loads(d["recipients"]) if d.get("recipients") else []
    if d.get("meeting"):
        d["meeting"] = json.loads(d["meeting"])
    body = d.get("body_text") or ""
    if body_chars is not None and len(body) > body_chars:
        d["body_text"] = body[:body_chars]
        d["body_truncated"] = True
    else:
        d["body_truncated"] = False
    return d


if __name__ == "__main__":
    # Smoke test: open the db, print what's in it.
    from console import use_utf8
    use_utf8()          # console.py

    conn = connect()
    print(f"{DB_PATH}: {counts(conn)}")
    for r in list_runs(conn, limit=5):
        print(f"  {r['id']}  {r['ok_count']} ok / {r['failed_count']} failed"
              f"  (missed {r['missed']})")
