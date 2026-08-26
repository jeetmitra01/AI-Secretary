"""
ADR-023: the privileged zone. The ONLY code in this system that can change
a calendar.

Everything about this file is arranged so that the model cannot reach it:

  - it is not in TOOL_FUNCTIONS, or in any other tool registry, and a test
    asserts that
  - it takes a proposal ID and reads the payload from SQLite. It never
    accepts prose, and it never reads an email body. A prompt injection
    that reached the proposing agent has, at this boundary, been reduced
    to a typed struct that a human or a deterministic gate must approve
  - it holds its own credentials, in token_write.json, so the scheduled
    digest and the chat agent run on a token that cannot write

Setup (one-off, same Google Cloud project as Gmail and Calendar):
    1. Add .../auth/calendar.events to the OAuth consent screen scopes
    2. python executor.py --consent      # mints token_write.json

Use:
    python executor.py                   # list pending proposals
    python executor.py <proposal-id>     # commit one, as a human

What is NOT here, on purpose: update, delete, and attendees (ADR-023). To
undo a committed event, delete it in Google Calendar. Every event this
file creates is `tentative` and carries the marker below, so agent-made
events are easy to find and obviously provisional.
"""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from datetime import datetime, timezone

from pydantic import ValidationError

import store
from auth import ReauthorizationRequired, load_credentials
from proposals import USER_TZ, CalendarProposal, evaluate

# `events` and not `calendar`: the full scope can delete entire calendars.
# There is no create-only scope, which is exactly why ADR-023 says the real
# restriction now lives in this file rather than in the grant.
CALENDAR_WRITE_SCOPES = ["https://www.googleapis.com/auth/calendar.events"]

# NOT token.json. ADR-018 kept Gmail and calendar.readonly on one token
# because both were read-only; that reasoning ends here. The unattended
# paths (scheduled digest, /chat) must not hold a credential that writes.
WRITE_TOKEN_FILE = "token_write.json"

# Shadow mode (ADR-024). Nothing commits without a human until this flips,
# and flipping it is a decision with evidence behind it — see the query in
# __main__ — not a config tweak.
AUTO_COMMIT = False

# Stamped into every created event. Two jobs: make agent-made events
# findable in a calendar full of human ones, and give a future undo path
# something narrow to scope itself to.
MARKER_KEY = "createdBy"
MARKER_VALUE = "secretary-agent"

# Failures that block ANY commit, human included. Everything else the gate
# reports is advisory for a human and binding for `auto`: the gate decides
# AUTONOMY, not permission (ADR-024). A person who reads
# "no_conflict (Board prep)" and confirms anyway has made an informed
# choice, which is the entire point of a human in the loop. A person cannot
# usefully "override" a payload that will not parse or an event in the past.
HARD_CHECKS = frozenset({"schema", "future"})


# --- the capability --------------------------------------------------------

class CalendarWriter(ABC):
    """Mirrors CalendarConnector (ADR-002/018), and is deliberately a
    SEPARATE class rather than a subclass of it. Inheritance would mean a
    read path could acquire write powers by construction; two classes mean
    holding one gives you nothing of the other."""

    @abstractmethod
    def create_event(self, proposal: CalendarProposal,
                     proposal_id: str) -> str:
        """Create one event. Returns the provider's event id."""


class GoogleCalendarWriter(CalendarWriter):
    source = "google"

    def __init__(self, credentials_file: str = "credentials.json",
                 token_file: str = WRITE_TOKEN_FILE,
                 allow_interactive_auth: bool = False,
                 calendar_id: str = "primary"):
        self.credentials_file = credentials_file
        self.token_file = token_file
        self.allow_interactive_auth = allow_interactive_auth
        self.calendar_id = calendar_id
        self._service = None

    def _get_service(self):
        if self._service is not None:
            return self._service
        from googleapiclient.discovery import build
        creds = load_credentials(CALENDAR_WRITE_SCOPES, self.credentials_file,
                                 self.token_file, self.allow_interactive_auth)
        self._service = build("calendar", "v3", credentials=creds)
        return self._service

    def create_event(self, proposal: CalendarProposal,
                     proposal_id: str) -> str:
        body = {
            "summary": proposal.title,
            "description": _provenance(proposal, proposal_id),
            # timeZone alongside an already-offset dateTime is not
            # redundant: it is what the event carries forward if it is ever
            # made recurring, and it makes the intended zone visible in the
            # UI instead of implied by an offset.
            "start": {"dateTime": proposal.start.isoformat(),
                      "timeZone": USER_TZ.key},
            "end": {"dateTime": proposal.end.isoformat(),
                    "timeZone": USER_TZ.key},
            # Tentative, always. A wrong event should look wrong at a
            # glance rather than sit there looking authoritative.
            "status": "tentative",
            "extendedProperties": {"private": {MARKER_KEY: MARKER_VALUE,
                                               "proposalId": proposal_id}},
        }
        # No attendees key at all — CalendarProposal forbids the field, and
        # sendUpdates="none" is the second belt: adding a guest makes Google
        # send them mail, and this system does not send mail (ADR-023).
        event = self._get_service().events().insert(
            calendarId=self.calendar_id, body=body, sendUpdates="none",
        ).execute()
        return event["id"]


def _provenance(proposal: CalendarProposal, proposal_id: str) -> str:
    """Written into the event body, so the calendar itself records where
    this came from. Useful months later, when the only surviving evidence
    is the event."""
    lines = [proposal.description or "",
             "",
             "— created by the secretary agent (tentative)",
             f"proposal: {proposal_id}",
             f"from the phrase: {proposal.source_time_text!r}"]
    if proposal.source_email_id:
        lines.append(f"source email: {proposal.source_email_id}")
    return "\n".join(lines).strip()


# --- the commit path -------------------------------------------------------

def commit(conn: sqlite3.Connection, proposal_id: str, actor: str = "human",
           writer: CalendarWriter | None = None, calendar=None,
           now: datetime | None = None) -> dict:
    """Turn one pending proposal into a real calendar event.

    The order below is the whole safety argument, so it is worth reading as
    a sequence rather than as steps:

      expire -> load -> re-parse -> re-run the gate -> CLAIM -> write

    Re-parsing and re-running the gate is not defensive duplication. The
    verdict stored on the row was computed when the proposal was written,
    and a stored verdict can be outrun: the calendar fills, the clock moves
    the event out of its window, the row is edited. The gate that decides
    is the one that runs now (ADR-024).

    The claim happens BEFORE the API call, so two confirms racing cannot
    both create an event.

    `writer` and `calendar` are injectable so tests never touch Google.
    """
    now = now or datetime.now(timezone.utc)
    store.expire_proposals(conn, now)

    row = store.get_proposal(conn, proposal_id)
    if row is None:
        return _result(proposal_id, "unknown", "no such proposal")
    if row["status"] != "pending":
        # Idempotent by design: confirming twice reports the first outcome
        # instead of creating a second event.
        return _result(proposal_id, row["status"],
                       f"already {row['status']}", event_id=row["event_id"])

    try:
        proposal = CalendarProposal.model_validate_json(row["payload"])
    except ValidationError as e:
        store.mark_proposal(conn, proposal_id, "failed", decided_by=actor,
                            error=f"schema: {e}")
        return _result(proposal_id, "failed", f"payload no longer valid: {e}")

    verdict = evaluate(proposal, conn, now=now, calendar=calendar,
                       exclude_id=proposal_id)
    blocking = [c for c in verdict.failed if c in HARD_CHECKS]
    if blocking:
        return _result(proposal_id, "pending",
                       f"blocked: {verdict.summary()}", verdict=verdict)
    if actor == "auto" and not verdict.ok:
        return _result(proposal_id, "pending",
                       f"not auto-eligible: {verdict.summary()}",
                       verdict=verdict)
    if actor == "auto" and not AUTO_COMMIT:
        return _result(proposal_id, "pending",
                       "auto-commit is off (shadow mode, ADR-024)",
                       verdict=verdict)

    if not store.claim_proposal(conn, proposal_id):
        current = store.get_proposal(conn, proposal_id) or {}
        return _result(proposal_id, current.get("status", "unknown"),
                       "another confirm got there first",
                       event_id=current.get("event_id"))

    writer = writer or GoogleCalendarWriter()
    try:
        event_id = writer.create_event(proposal, proposal_id)
    except ReauthorizationRequired as e:
        # Retryable, so the proposal goes back to pending with the reason
        # attached rather than dying. Only a human at a terminal can fix
        # this, and the message says how.
        store.mark_proposal(conn, proposal_id, "pending",
                            error=f"reauthorization required: {e}",
                            from_status="committing")
        return _result(proposal_id, "pending",
                       f"calendar write needs consent. ({e})",
                       needs_consent=True)
    except Exception as e:
        store.mark_proposal(conn, proposal_id, "failed", decided_by=actor,
                            error=f"{type(e).__name__}: {e}",
                            from_status="committing")
        return _result(proposal_id, "failed",
                       f"calendar write failed: {type(e).__name__}: {e}")

    store.mark_proposal(conn, proposal_id, "confirmed", decided_by=actor,
                        event_id=event_id, from_status="committing")
    return _result(proposal_id, "confirmed",
                   f"created (tentative): {proposal.human()}",
                   event_id=event_id, verdict=verdict)


def reject(conn: sqlite3.Connection, proposal_id: str,
           reason: str = "") -> dict:
    """Decline a proposal. Rejections are kept, not deleted: shadow mode
    needs the denominator as much as the numerator (ADR-024)."""
    row = store.get_proposal(conn, proposal_id)
    if row is None:
        return _result(proposal_id, "unknown", "no such proposal")
    if row["status"] != "pending":
        return _result(proposal_id, row["status"], f"already {row['status']}")

    store.mark_proposal(conn, proposal_id, "rejected", decided_by="human",
                        error=reason or None)
    return _result(proposal_id, "rejected", "rejected")


def _result(proposal_id: str, status: str, message: str,
            event_id: str | None = None, verdict=None,
            needs_consent: bool = False) -> dict:
    return {
        "proposal_id": proposal_id,
        "status": status,
        "message": message,
        "event_id": event_id,
        # A flag rather than a string the caller has to grep: server.py
        # turns this into a 503, the same status ingest uses when only a
        # human at a terminal can fix the grant.
        "needs_consent": needs_consent,
        "policy_ok": None if verdict is None else verdict.ok,
        "policy_failed": [] if verdict is None else list(verdict.failed),
        "policy_summary": None if verdict is None else verdict.summary(),
    }


# --- entrypoint ------------------------------------------------------------

if __name__ == "__main__":
    import json
    import sys

    from console import use_utf8
    use_utf8()          # event titles and sender names are arbitrary
                        # Unicode (ADR-021)

    args = sys.argv[1:]

    if args and args[0] == "--consent":
        # The one place a human is definitionally present, so this is where
        # the write scope gets consented to. Same shape as calendars.py.
        GoogleCalendarWriter(allow_interactive_auth=True)._get_service()
        print(f"ok: {WRITE_TOKEN_FILE} now holds {CALENDAR_WRITE_SCOPES[0]}")
        sys.exit(0)

    conn = store.connect()
    try:
        store.expire_proposals(conn)

        if args:
            print(json.dumps(commit(conn, args[0]), indent=2))
            sys.exit(0)

        pending = store.list_proposals(conn, status="pending")
        print(f"{len(pending)} pending proposal(s)  "
              f"[AUTO_COMMIT={AUTO_COMMIT}]")
        for row in pending:
            proposal = CalendarProposal.model_validate_json(row["payload"])
            failed = json.loads(row["policy_failed"] or "[]")
            print(f"  {row['id']}  {proposal.human()}")
            print(f"{'':>12}{'eligible' if row['policy_ok'] else 'needs review'}"
                  f"{': ' + ', '.join(failed) if failed else ''}")

        # The shadow-mode question, in one query (ADR-024): of the
        # proposals the gate called eligible, how many did a human confirm?
        # That ratio is what earns AUTO_COMMIT, and nothing else does.
        agreed = conn.execute(
            """SELECT status, COUNT(*) AS n FROM proposals
                WHERE policy_ok = 1 AND status IN ('confirmed','rejected')
                GROUP BY status""").fetchall()
        if agreed:
            print("\nshadow mode — human decisions on gate-eligible "
                  "proposals:")
            for r in agreed:
                print(f"  {r['status']:>9}: {r['n']}")
    finally:
        conn.close()
