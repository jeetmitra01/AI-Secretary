"""
Tests for the two-zone calendar write path (ADR-023, ADR-024).

The split here mirrors tests/test_console.py:

  1. INVARIANT — the write capability is not in the model's tool registry.
     This is the test that actually enforces ADR-023. Everything else in
     this file checks that the machinery works; only this one catches the
     future refactor that "simplifies" things by registering
     executor.commit as a tool.

  2. BEHAVIOUR — the gate, the expiry, and the commit path, all against a
     temporary database and a fake writer. Nothing here touches Google or
     Anthropic.

Run:  python -m pytest tests -q
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import executor                                        # noqa: E402
import proposals as P                                  # noqa: E402
import store                                           # noqa: E402
from models import Email                               # noqa: E402


# --- fixtures --------------------------------------------------------------

@pytest.fixture
def conn(tmp_path, monkeypatch):
    """A real SQLite database, in a temp dir. store.DB_PATH is monkeypatched
    rather than passed as an argument because connect() resolves the module
    constant at call time precisely so tests can redirect it."""
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "test.db")
    connection = store.connect()
    yield connection
    connection.close()


class FakeCalendar:
    """Stands in for the ADR-018 read connector."""

    def __init__(self, blocks=None, error=None):
        self.blocks = blocks or []
        self.error = error

    def busy_blocks(self, day, tz):
        if self.error:
            raise self.error
        return self.blocks


class FakeWriter(executor.CalendarWriter):
    """Records what would have been sent to Google, and can fail on cue."""

    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def create_event(self, proposal, proposal_id):
        if self.error:
            raise self.error
        self.calls.append((proposal, proposal_id))
        return f"evt-{proposal_id}"


NOW = datetime(2026, 8, 23, 6, 0, tzinfo=timezone.utc)   # 11:30 local


def a_proposal(**overrides):
    """A proposal that passes every check, unless a test breaks one."""
    start = NOW + timedelta(days=2)          # inside lead window
    fields = dict(
        title="Call with Priya",
        start=start,
        end=start + timedelta(minutes=30),
        source_time_text="Tuesday at 11:30am IST",
        source_email_id="email-1",
        confidence="high",
    )
    fields.update(overrides)
    return P.CalendarProposal(**fields)


def seed_sender(conn, count=2, email_id="email-1"):
    """MIN_SENDER_HISTORY emails from one address, so known_sender passes."""
    emails = [
        Email(id=email_id if i == 0 else f"{email_id}-{i}",
              thread_id="t1", sender="Priya <priya@example.com>",
              subject="Meeting?", body_text="Can we talk Tuesday?",
              received_at=NOW - timedelta(days=i + 1), source="gmail")
        for i in range(count)
    ]
    store.save_emails(conn, emails, run_id="test-run")


# --- 1. the invariant ------------------------------------------------------

def test_write_capability_is_not_in_the_tool_registry():
    """ADR-023's actual enforcement point.

    The model can only call what is in TOOL_FUNCTIONS. If a calendar write
    ever appears there, an instruction inside an untrusted email body has a
    live path to the calendar and the whole two-zone design is decoration.
    """
    from secretary_agent import TOOL_FUNCTIONS

    registered = set(TOOL_FUNCTIONS.values())
    for name in ("commit", "reject", "GoogleCalendarWriter"):
        assert getattr(executor, name) not in registered, (
            f"executor.{name} is reachable from the model's tool registry")

    # And by name, for the version of this mistake that wraps it in a lambda
    # or renames it on the way in.
    for tool_name in TOOL_FUNCTIONS:
        assert "commit" not in tool_name
        module = getattr(TOOL_FUNCTIONS[tool_name], "__module__", "")
        assert module != "executor", (
            f"tool {tool_name!r} is defined in executor.py, the privileged "
            f"zone — the model must not be able to call into it")


def test_proposal_schema_refuses_attendees():
    """extra='forbid' is a control, not tidiness: an attendee makes Google
    send mail, and this system does not send mail (ADR-023)."""
    with pytest.raises(ValidationError):
        a_proposal(attendees=["someone@example.com"])


def test_auto_commit_ships_off():
    """Shadow mode (ADR-024). Flipping this needs evidence and an ADR."""
    assert executor.AUTO_COMMIT is False


# --- 2. the payload --------------------------------------------------------

def test_naive_datetimes_are_rejected():
    """A naive datetime would be read as UTC and land 5h30 from where the
    user meant — the exact failure ADR-023 calls out."""
    with pytest.raises(ValidationError):
        a_proposal(start=datetime(2026, 8, 25, 14, 30))


def test_end_must_follow_start():
    with pytest.raises(ValidationError):
        a_proposal(end=NOW - timedelta(days=1))


def test_human_line_shows_the_verbatim_phrase():
    """The mechanism a person uses to catch a timezone misread: the words
    from the email printed beside the instant they were resolved to."""
    assert "Tuesday at 11:30am IST" in a_proposal().human()


# --- 3. the gate -----------------------------------------------------------

def test_clean_proposal_is_eligible(conn):
    seed_sender(conn)
    verdict = P.evaluate(a_proposal(), conn, now=NOW,
                         calendar=FakeCalendar([]))
    assert verdict.ok, verdict.summary()


def test_unknown_sender_fails(conn):
    """No email from this person in the store. An email body cannot argue
    with this check, which is the point (ADR-024)."""
    verdict = P.evaluate(a_proposal(), conn, now=NOW,
                         calendar=FakeCalendar([]))
    assert "known_sender" in verdict.failed


def test_single_email_from_sender_is_not_enough(conn):
    seed_sender(conn, count=1)
    verdict = P.evaluate(a_proposal(), conn, now=NOW,
                         calendar=FakeCalendar([]))
    assert "known_sender" in verdict.failed


def test_conflict_blocks(conn):
    seed_sender(conn)
    verdict = P.evaluate(a_proposal(), conn, now=NOW,
                         calendar=FakeCalendar([
                             {"title": "Board prep", "all_day": False,
                              "start": "11:00", "end": "12:00"}]))
    assert "no_conflict" in verdict.failed
    assert "Board prep" in verdict.notes["no_conflict"]


def test_non_overlapping_block_does_not_conflict(conn):
    seed_sender(conn)
    verdict = P.evaluate(a_proposal(), conn, now=NOW,
                         calendar=FakeCalendar([
                             {"title": "Standup", "all_day": False,
                              "start": "09:00", "end": "09:15"}]))
    assert "no_conflict" not in verdict.failed


def test_all_day_block_counts_as_a_conflict(conn):
    """Deliberately conservative: 'you are on holiday' is exactly when a
    human should look."""
    seed_sender(conn)
    verdict = P.evaluate(a_proposal(), conn, now=NOW,
                         calendar=FakeCalendar([
                             {"title": "Vacation", "all_day": True,
                              "date": "2026-08-25"}]))
    assert "no_conflict" in verdict.failed


def test_calendar_lookup_failure_fails_closed(conn):
    """ADR-018's rule, carried forward: a broken lookup must never read as
    a clear day."""
    seed_sender(conn)
    verdict = P.evaluate(a_proposal(), conn, now=NOW,
                         calendar=FakeCalendar(error=RuntimeError("boom")))
    assert "no_conflict" in verdict.failed


def test_out_of_hours_fails(conn):
    seed_sender(conn)
    start = datetime(2026, 8, 25, 20, 0, tzinfo=timezone.utc)   # 01:30 local
    verdict = P.evaluate(
        a_proposal(start=start, end=start + timedelta(minutes=30)),
        conn, now=NOW, calendar=FakeCalendar([]))
    assert "working_hours" in verdict.failed


def test_overlong_meeting_fails(conn):
    seed_sender(conn)
    start = NOW + timedelta(days=2)
    verdict = P.evaluate(
        a_proposal(start=start, end=start + timedelta(hours=9)),
        conn, now=NOW, calendar=FakeCalendar([]))
    assert "duration" in verdict.failed


def test_far_future_fails(conn):
    seed_sender(conn)
    start = NOW + timedelta(days=90)
    verdict = P.evaluate(
        a_proposal(start=start, end=start + timedelta(minutes=30)),
        conn, now=NOW, calendar=FakeCalendar([]))
    assert "lead_time" in verdict.failed


def test_past_event_fails(conn):
    seed_sender(conn)
    start = NOW - timedelta(days=1)
    verdict = P.evaluate(
        a_proposal(start=start, end=start + timedelta(minutes=30)),
        conn, now=NOW, calendar=FakeCalendar([]))
    assert "future" in verdict.failed


def test_confidence_can_only_veto(conn):
    """'high' does not grant eligibility on its own — every other check
    still has to pass — but anything less blocks it (ADR-024)."""
    seed_sender(conn)
    verdict = P.evaluate(a_proposal(confidence="medium"), conn, now=NOW,
                         calendar=FakeCalendar([]))
    assert "confidence" in verdict.failed
    assert not verdict.ok


def test_flooding_from_one_email_is_capped(conn):
    seed_sender(conn)
    for _ in range(P.MAX_PENDING_PER_EMAIL):
        store.save_proposal(conn, a_proposal().model_dump_json(),
                            P.expires_at(NOW), True, [],
                            source_email_id="email-1")
    verdict = P.evaluate(a_proposal(), conn, now=NOW,
                         calendar=FakeCalendar([]))
    assert "not_flooding" in verdict.failed


# --- 4. the commit path ----------------------------------------------------

def save(conn, proposal, verdict_ok=True, failed=None, expires=None):
    return store.save_proposal(
        conn, proposal.model_dump_json(), expires or P.expires_at(NOW),
        verdict_ok, failed or [], source_email_id=proposal.source_email_id)


def test_commit_creates_a_tentative_marked_event(conn):
    seed_sender(conn)
    proposal_id = save(conn, a_proposal())
    writer = FakeWriter()

    result = executor.commit(conn, proposal_id, writer=writer,
                             calendar=FakeCalendar([]), now=NOW)

    assert result["status"] == "confirmed"
    assert result["event_id"] == f"evt-{proposal_id}"
    assert len(writer.calls) == 1
    assert store.get_proposal(conn, proposal_id)["event_id"] == result["event_id"]


def test_committing_twice_does_not_create_two_events(conn):
    seed_sender(conn)
    proposal_id = save(conn, a_proposal())
    writer = FakeWriter()

    first = executor.commit(conn, proposal_id, writer=writer,
                            calendar=FakeCalendar([]), now=NOW)
    second = executor.commit(conn, proposal_id, writer=writer,
                             calendar=FakeCalendar([]), now=NOW)

    assert first["status"] == "confirmed"
    assert second["status"] == "confirmed"
    assert second["message"].startswith("already")
    assert len(writer.calls) == 1, "the second confirm created a second event"


def test_expired_proposal_cannot_be_committed(conn):
    seed_sender(conn)
    proposal_id = save(conn, a_proposal(),
                       expires=NOW - timedelta(hours=1))
    writer = FakeWriter()

    result = executor.commit(conn, proposal_id, writer=writer,
                             calendar=FakeCalendar([]), now=NOW)

    assert result["status"] == "expired"
    assert not writer.calls


def test_a_human_may_commit_over_a_soft_policy_failure(conn):
    """The gate decides autonomy, not permission. Someone who reads
    'no_conflict (Board prep)' and confirms anyway has made an informed
    choice — that is what a human in the loop is for (ADR-024)."""
    seed_sender(conn)
    proposal_id = save(conn, a_proposal(), verdict_ok=False,
                       failed=["no_conflict"])
    writer = FakeWriter()

    result = executor.commit(conn, proposal_id, actor="human", writer=writer,
                             calendar=FakeCalendar([
                                 {"title": "Board prep", "all_day": False,
                                  "start": "11:00", "end": "12:00"}]), now=NOW)

    assert result["status"] == "confirmed"
    assert result["policy_ok"] is False, "the failure should still be reported"


def test_a_human_may_not_commit_an_event_in_the_past(conn):
    """HARD_CHECKS: there is nothing for a human to informedly override."""
    seed_sender(conn)
    start = NOW - timedelta(days=1)
    proposal_id = save(conn, a_proposal(start=start,
                                        end=start + timedelta(minutes=30)))
    writer = FakeWriter()

    result = executor.commit(conn, proposal_id, actor="human", writer=writer,
                             calendar=FakeCalendar([]), now=NOW)

    assert result["status"] == "pending"
    assert "future" in result["policy_failed"]
    assert not writer.calls


def test_auto_actor_is_refused_while_shadow_mode_is_on(conn):
    seed_sender(conn)
    proposal_id = save(conn, a_proposal())
    writer = FakeWriter()

    result = executor.commit(conn, proposal_id, actor="auto", writer=writer,
                             calendar=FakeCalendar([]), now=NOW)

    assert result["status"] == "pending"
    assert not writer.calls


def test_the_gate_reruns_at_commit_time(conn):
    """The stored verdict said eligible; the calendar has filled since. The
    verdict that decides is the one computed now (ADR-024)."""
    seed_sender(conn)
    proposal_id = save(conn, a_proposal(), verdict_ok=True, failed=[])
    writer = FakeWriter()

    result = executor.commit(conn, proposal_id, actor="auto", writer=writer,
                             calendar=FakeCalendar([
                                 {"title": "Board prep", "all_day": False,
                                  "start": "11:00", "end": "12:00"}]), now=NOW)

    assert result["status"] == "pending"
    assert "no_conflict" in result["policy_failed"]
    assert not writer.calls


def test_reauthorization_returns_the_proposal_to_pending(conn):
    """A dead write token is retryable, so the proposal must survive it."""
    from connectors import ReauthorizationRequired

    seed_sender(conn)
    proposal_id = save(conn, a_proposal())
    writer = FakeWriter(error=ReauthorizationRequired("token lacks scope"))

    result = executor.commit(conn, proposal_id, writer=writer,
                             calendar=FakeCalendar([]), now=NOW)

    assert result["status"] == "pending"
    assert result["needs_consent"] is True
    row = store.get_proposal(conn, proposal_id)
    assert row["status"] == "pending", "a retryable failure lost the proposal"
    assert "reauthorization" in row["error"]


def test_a_failed_write_does_not_leave_the_row_committing(conn):
    seed_sender(conn)
    proposal_id = save(conn, a_proposal())
    writer = FakeWriter(error=RuntimeError("500 from Google"))

    result = executor.commit(conn, proposal_id, writer=writer,
                             calendar=FakeCalendar([]), now=NOW)

    assert result["status"] == "failed"
    assert store.get_proposal(conn, proposal_id)["status"] == "failed"


def test_reject_keeps_the_row(conn):
    """Shadow mode needs the denominator as much as the numerator."""
    seed_sender(conn)
    proposal_id = save(conn, a_proposal())

    executor.reject(conn, proposal_id, "wrong day")

    row = store.get_proposal(conn, proposal_id)
    assert row["status"] == "rejected"
    assert row["error"] == "wrong day"


def test_rejected_proposal_cannot_then_be_committed(conn):
    seed_sender(conn)
    proposal_id = save(conn, a_proposal())
    executor.reject(conn, proposal_id, "no")
    writer = FakeWriter()

    result = executor.commit(conn, proposal_id, writer=writer,
                             calendar=FakeCalendar([]), now=NOW)

    assert result["status"] == "rejected"
    assert not writer.calls


def test_unknown_proposal_is_reported_not_raised(conn):
    result = executor.commit(conn, "deadbeef", writer=FakeWriter(),
                             calendar=FakeCalendar([]), now=NOW)
    assert result["status"] == "unknown"


# --- 5. what actually reaches the provider ---------------------------------

def test_event_body_is_tentative_marked_and_guestless(conn):
    """The one test that reads the dict we would hand to Google."""
    proposal = a_proposal()
    captured = {}

    class CapturingWriter(executor.GoogleCalendarWriter):
        def _get_service(self):
            outer = captured

            class Events:
                def insert(self, calendarId, body, sendUpdates):
                    outer["body"] = body
                    outer["sendUpdates"] = sendUpdates

                    class Req:
                        def execute(self):
                            return {"id": "evt-1"}
                    return Req()

            class Service:
                def events(self):
                    return Events()
            return Service()

    CapturingWriter().create_event(proposal, "abc123")

    body = captured["body"]
    assert body["status"] == "tentative"
    assert "attendees" not in body
    assert captured["sendUpdates"] == "none"
    assert (body["extendedProperties"]["private"][executor.MARKER_KEY]
            == executor.MARKER_VALUE)
    assert body["extendedProperties"]["private"]["proposalId"] == "abc123"
    assert body["start"]["timeZone"] == P.USER_TZ.key
    # Provenance in the event body: months later this is the only surviving
    # record of where the event came from.
    assert "Tuesday at 11:30am IST" in body["description"]
    assert "email-1" in body["description"]
