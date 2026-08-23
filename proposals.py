"""
ADR-023/024: the typed thing that crosses between the two trust zones, and
the deterministic gate that judges it.

The agent that reads email produces a CalendarProposal. `executor.py`
consumes one. Nothing else passes between them — in particular no prose,
because prose derived from an email body would carry an injection across
the boundary intact and the split would buy nothing (ADR-023).

Everything here is stdlib + pydantic. No Anthropic client, no Google
client at import time. That is deliberate: this module is imported by
BOTH zones, so anything heavy in it would leak one zone's dependencies
into the other.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from email.utils import parseaddr
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

# The principal's timezone. Single source of truth: secretary_agent.py
# imports this rather than declaring its own, because "what time is 2:30pm"
# must not be able to differ between the zone that proposes and the zone
# that commits.
USER_TZ = ZoneInfo("Asia/Kolkata")


# --- the payload -----------------------------------------------------------

class CalendarProposal(BaseModel):
    """One proposed event. Absolute times only.

    extra="forbid" is a security control, not tidiness. The field this
    schema most needs to NOT have is `attendees`: Google emails an
    invitation to anyone listed, which would be outbound mail from a
    system whose whole posture is "no sending" (ADR-023). Forbidding
    extras means a model that invents the field gets a validation error
    instead of a silently ignored one — and we find out.
    """
    model_config = ConfigDict(extra="forbid")

    title: str
    start: datetime
    end: datetime
    description: str | None = None

    # The verbatim phrase the time came from ("Monday at 2:30pm EST").
    # extraction.py keeps proposed times unnormalized on purpose; the agent
    # does the normalization for a proposal, so this is the receipt. The
    # confirmation view prints it NEXT TO the normalized instant, which is
    # how a human catches a confident timezone misread (ADR-023).
    source_time_text: str
    source_email_id: str | None = None

    # Kept, and deliberately demoted. ADR-024: this is a token the model
    # emitted, not a measurement, and the email body can influence it. It
    # can veto autonomy; it can never grant it alone.
    confidence: Literal["high", "medium", "low"] = "medium"

    @field_validator("start", "end")
    @classmethod
    def _must_be_aware(cls, v: datetime) -> datetime:
        # A naive datetime here is the exact bug this system cannot afford:
        # it would be read as UTC by the API and land 5h30 from where the
        # user meant. Rejecting is the only safe reading.
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("must include a UTC offset, e.g. "
                             "2026-08-25T14:30:00+05:30")
        return v

    @model_validator(mode="after")
    def _end_after_start(self):
        if self.end <= self.start:
            raise ValueError("end must be after start")
        return self

    def local(self) -> tuple[datetime, datetime]:
        return self.start.astimezone(USER_TZ), self.end.astimezone(USER_TZ)

    def human(self) -> str:
        """One line for the confirmation view. Shows the verbatim phrase
        beside the normalized instant — see source_time_text."""
        start_local, end_local = self.local()
        return (f"{start_local:%a %d %b %Y %H:%M}-{end_local:%H:%M} "
                f"{USER_TZ.key}  {self.title!r}   "
                f"(from: {self.source_time_text!r})")


# --- the gate --------------------------------------------------------------
#
# Thresholds are constants, not config. Changing one is a policy change and
# should appear in a commit message, not in a file nobody reads (ADR-024).

MIN_LEAD = timedelta(hours=1)      # no "book it right now" bookings
MAX_LEAD = timedelta(days=14)
MIN_DURATION = timedelta(minutes=15)
MAX_DURATION = timedelta(minutes=120)
DAY_START = time(7, 0)             # local; a 3am meeting is a tz misread
DAY_END = time(21, 0)
MIN_SENDER_HISTORY = 2             # emails from this sender already stored
MAX_PENDING_PER_EMAIL = 3
PROPOSAL_TTL = timedelta(hours=48)


@dataclass
class Verdict:
    """A list of failed check NAMES, not a score.

    A single number invites an argument about the threshold and hides
    which check failed. Names are auditable, and they are what the human
    reads at confirmation time (ADR-024).
    """
    failed: list[str] = field(default_factory=list)
    notes: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.failed

    def fail(self, check: str, note: str = "") -> None:
        if check not in self.failed:
            self.failed.append(check)
        if note:
            self.notes[check] = note

    def summary(self) -> str:
        if self.ok:
            return "eligible"
        return "needs review: " + ", ".join(
            f"{c} ({self.notes[c]})" if c in self.notes else c
            for c in self.failed)


def evaluate(proposal: CalendarProposal, conn: sqlite3.Connection,
             now: datetime | None = None, calendar=None,
             exclude_id: str | None = None) -> Verdict:
    """Judge a proposal against facts we own.

    Runs TWICE in the life of a proposal: once when it is written (so the
    verdict can be recorded and explained), and again inside the executor
    immediately before the API call. The second run is the one that
    matters — a verdict computed hours ago can be outrun by the calendar
    filling up or by the clock moving the event out of its window
    (ADR-024). Every failure path here fails CLOSED.

    `calendar` is injectable so tests never touch Google. Passing None
    builds the ADR-018 read-only connector lazily.
    """
    now = now or datetime.now(timezone.utc)
    verdict = Verdict()

    _check_timing(proposal, now, verdict)
    _check_sender(proposal, conn, verdict)
    _check_flooding(proposal, conn, exclude_id, verdict)
    _check_conflict(proposal, calendar, verdict)

    # Demoted, per ADR-024: "high" is necessary and nowhere near
    # sufficient — it only counts once every check above has passed.
    if proposal.confidence != "high":
        verdict.fail("confidence", f"model said {proposal.confidence}")

    return verdict


def _check_timing(p: CalendarProposal, now: datetime, v: Verdict) -> None:
    lead = p.start - now
    if lead <= timedelta(0):
        v.fail("future", "starts in the past")
    elif lead < MIN_LEAD:
        v.fail("lead_time", f"starts in {lead}, under {MIN_LEAD}")
    elif lead > MAX_LEAD:
        v.fail("lead_time", f"starts in {lead}, over {MAX_LEAD}")

    duration = p.end - p.start
    if not (MIN_DURATION <= duration <= MAX_DURATION):
        v.fail("duration",
               f"{duration} outside {MIN_DURATION}-{MAX_DURATION}")

    start_local, end_local = p.local()
    # The date comparison catches midnight crossings, which would otherwise
    # pass the two time comparisons while spanning two days.
    if (start_local.time() < DAY_START or end_local.time() > DAY_END
            or start_local.date() != end_local.date()):
        v.fail("working_hours",
               f"{start_local:%H:%M}-{end_local:%H:%M} local outside "
               f"{DAY_START:%H:%M}-{DAY_END:%H:%M}")


def _check_sender(p: CalendarProposal, conn: sqlite3.Connection,
                  v: Verdict) -> None:
    """Has this person written before? A query against our own store, which
    is precisely why an email body cannot argue with it (ADR-024)."""
    if not p.source_email_id:
        v.fail("known_sender", "no source email cited")
        return

    row = conn.execute("SELECT sender FROM emails WHERE id = ?",
                       (p.source_email_id,)).fetchone()
    if row is None:
        v.fail("known_sender", "source email not in the store")
        return

    address = parseaddr(row["sender"])[1].lower()
    if not address:
        v.fail("known_sender", "unparseable sender")
        return

    # LIKE metacharacters are legal in the local part of an address, so the
    # pattern is escaped rather than interpolated raw.
    escaped = (address.replace("\\", "\\\\")
                      .replace("%", "\\%")
                      .replace("_", "\\_"))
    seen = conn.execute(
        r"SELECT COUNT(*) FROM emails WHERE lower(sender) LIKE ? ESCAPE '\'",
        (f"%{escaped}%",)).fetchone()[0]
    if seen < MIN_SENDER_HISTORY:
        v.fail("known_sender", f"{seen} email(s) from {address}")


def _check_flooding(p: CalendarProposal, conn: sqlite3.Connection,
                    exclude_id: str | None, v: Verdict) -> None:
    """One email should not be able to fill the queue."""
    if not p.source_email_id:
        return                      # already failed known_sender
    pending = conn.execute(
        """SELECT COUNT(*) FROM proposals
           WHERE source_email_id = ? AND status = 'pending'
             AND id IS NOT ?""",
        (p.source_email_id, exclude_id)).fetchone()[0]
    if pending >= MAX_PENDING_PER_EMAIL:
        v.fail("not_flooding", f"{pending} already pending from that email")


def _check_conflict(p: CalendarProposal, calendar, v: Verdict) -> None:
    """Ground truth from the read-only calendar (ADR-018).

    A lookup FAILURE is a failed check, not a pass. ADR-018 made the same
    call for busy_blocks: an empty list means free, and a broken lookup
    must never read as a clear day.
    """
    start_local, end_local = p.local()
    try:
        if calendar is None:
            from calendars import CALENDARS
            calendar = CALENDARS["google"]()
        blocks = calendar.busy_blocks(start_local.date(), USER_TZ)
    except Exception as e:
        v.fail("no_conflict",
               f"calendar lookup failed: {type(e).__name__}: {e}")
        return

    clashes = [b.get("title", "(no title)") for b in blocks
               if _overlaps(b, start_local, end_local)]
    if clashes:
        v.fail("no_conflict", "; ".join(clashes[:3]))


def _overlaps(block: dict, start_local: datetime, end_local: datetime) -> bool:
    """An all-day block counts as a conflict.

    Arguable — an all-day 'Vacation' does not literally occupy 15:00-16:00.
    But this gate decides whether to act with no human present, and "you
    are on holiday that day" is exactly when a human should look. Failing
    closed is the whole posture (ADR-024).
    """
    if block.get("all_day"):
        return True

    day = start_local.date()
    try:
        block_start = datetime.combine(
            day, time.fromisoformat(block["start"]), tzinfo=USER_TZ)
        block_end = datetime.combine(
            day, time.fromisoformat(block["end"]), tzinfo=USER_TZ)
    except (KeyError, ValueError):
        return True                 # unreadable block: assume it conflicts

    return block_start < end_local and start_local < block_end


def expires_at(created: datetime | None = None) -> datetime:
    """A pending proposal is not confirmable forever. A stale one that an
    injection planted must not be confirmable by a tired human next week
    (ADR-024)."""
    return (created or datetime.now(timezone.utc)) + PROPOSAL_TTL
