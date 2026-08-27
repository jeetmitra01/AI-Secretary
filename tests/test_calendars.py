"""
Tests for the calendar normalization layer (calendars.py, ADR-018).

The spec is already written: the `CalendarConnector` docstring lists six
promises to callers. Each one is a test below, because each one is a
promise the agent RELIES on when it tells the principal "you are free".

No network, no credentials, no googleapiclient. `_get_service` caches
into `self._service`, so setting that field is a real seam: the tests
drive the actual `busy_blocks` filter and normalize logic with a fake
transport, and never touch token.json.

What makes these worth writing: every filter here removes an event, and
a removed event is invisible. A broken filter does not raise — it
reports a clear day. That is the same silent-negative class as ADR-012,
and the only thing that catches it is a test that names the event it
expects to survive.

Run:  python -m pytest tests -q
"""

from __future__ import annotations

from datetime import date
from zoneinfo import ZoneInfo

import pytest

from calendars import GoogleCalendarConnector

# The principal's real timezone (composition.USER_TZ). +05:30 is not a
# whole number of hours, so a connector that slices the ISO string
# instead of converting gives an obviously wrong answer, not a plausible
# one.
TZ = ZoneInfo("Asia/Kolkata")
DAY = date(2026, 8, 27)


# --- the fake transport ----------------------------------------------------

class _FakeRequest:
    def __init__(self, response: dict, error: Exception | None = None):
        self._response, self._error = response, error

    def execute(self) -> dict:
        if self._error:
            raise self._error
        return self._response


class _FakeService:
    """Stands in for the googleapiclient resource.

    It records every list() kwarg, because half the contract is about the
    QUERY, not the result: `singleEvents` and the timeMin/timeMax bounds
    cannot be seen in the returned blocks at all. A test that only
    checked the output would pass with recurrence expansion turned off.
    """

    def __init__(self, items_by_calendar: dict[str, list[dict]],
                 error: Exception | None = None):
        self.items_by_calendar = items_by_calendar
        self.error = error
        self.calls: list[dict] = []

    def events(self):
        return self

    def list(self, **kwargs):
        self.calls.append(kwargs)
        items = self.items_by_calendar.get(kwargs["calendarId"], [])
        return _FakeRequest({"items": items}, self.error)


def connector(items, calendar_ids=("primary",),
              error: Exception | None = None) -> GoogleCalendarConnector:
    if isinstance(items, list):
        items = {"primary": items}
    cal = GoogleCalendarConnector(calendar_ids=calendar_ids)
    cal._service = _FakeService(items, error)   # the seam; no auth runs
    return cal


# --- event builders, shaped like real Google Calendar API payloads ---------

def timed(title: str, start: str, end: str, **extra) -> dict:
    """A timed event. Google always sends an OFFSET on dateTime."""
    return {"summary": title,
            "start": {"dateTime": start}, "end": {"dateTime": end},
            **extra}


def all_day(title: str, start: str, end: str, **extra) -> dict:
    """An all-day event carries `date`, never `dateTime`. The two are
    never both present, and reading the wrong one is a KeyError on
    exactly the events nobody tests with: birthdays, PTO, holidays.
    """
    return {"summary": title,
            "start": {"date": start}, "end": {"date": end},
            **extra}


BUSY = timed("Board prep", "2026-08-27T15:00:00+05:30",
             "2026-08-27T16:00:00+05:30")


# --- 1. the base case, so the filter tests below cannot be vacuous ---------

def test_an_unmarked_event_is_busy():
    """The guard on the guard, in the shape test_console.py uses.

    Every filter test below starts from BUSY and adds one marker. If BUSY
    itself stopped coming back — a typo in the fake, a changed payload
    shape — those tests would all still pass while proving nothing.
    """
    blocks = connector([BUSY]).busy_blocks(DAY, TZ)

    assert len(blocks) == 1, "the unmarked base event must be busy"
    assert blocks[0]["title"] == "Board prep"


# --- 2. an empty list means FREE, and never "the lookup failed" ------------

def test_no_events_means_free():
    assert connector([]).busy_blocks(DAY, TZ) == []


def test_a_failed_lookup_raises_instead_of_reading_as_free():
    """The ADR-018 consequence, and the reason it is written down. If
    busy_blocks swallowed an API error and returned [], a broken calendar
    would look exactly like a clear day, and the agent would tell the
    principal to accept a meeting on top of a real one.
    """
    cal = connector([BUSY], error=RuntimeError("500 backendError"))

    with pytest.raises(RuntimeError):
        cal.busy_blocks(DAY, TZ)


# --- 3. normalization: provider payload -> the block the agent sees --------

def test_a_timed_event_normalizes_to_the_documented_shape():
    """The tool schema in secretary_agent.py promises exactly these keys."""
    block, = connector([BUSY]).busy_blocks(DAY, TZ)

    assert block == {"title": "Board prep", "all_day": False,
                     "start": "15:00", "end": "16:00", "calendar": "primary"}


def test_clock_times_are_converted_into_the_callers_timezone():
    """Converted, not sliced out of the payload. The caller owns the
    timezone, so a UTC payload must render as Kolkata wall time: 09:00Z
    is 14:30 local, and 14:30 cannot appear by accident.
    """
    utc_event = timed("Sync with NY", "2026-08-27T09:00:00+00:00",
                      "2026-08-27T10:00:00+00:00")

    block, = connector([utc_event]).busy_blocks(DAY, TZ)

    assert (block["start"], block["end"]) == ("14:30", "15:30")


def test_an_event_with_no_title_does_not_kill_the_run():
    """`summary` is ABSENT, not empty, on an event created without one.
    A KeyError here would end the whole digest over one untitled block.
    """
    untitled = {"start": {"dateTime": "2026-08-27T15:00:00+05:30"},
                "end": {"dateTime": "2026-08-27T16:00:00+05:30"}}

    block, = connector([untitled]).busy_blocks(DAY, TZ)

    assert block["title"] == "(no title)"


def test_an_all_day_event_reports_no_clock_times():
    """Reporting "busy 00:00-23:59" would be a lie about an all-day
    'Vacation', so the contract says all_day=True and no clock times. The
    agent branches on `all_day`; a start/end left on the block would read
    as a real window and be used to offer a slot inside a holiday.
    """
    block, = connector([all_day("Vacation", "2026-08-27",
                                "2026-08-28")]).busy_blocks(DAY, TZ)

    assert block["all_day"] is True
    assert "start" not in block and "end" not in block
    assert block["date"] == "2026-08-27"


# --- 4. the filters: everything returned must be a REAL conflict -----------

def test_an_event_the_principal_declined_is_not_busy():
    declined = BUSY | {"attendees": [
        {"email": "me@example.com", "self": True,
         "responseStatus": "declined"}]}

    assert connector([declined]).busy_blocks(DAY, TZ) == []


def test_an_event_another_guest_declined_is_still_busy():
    """The bug the `self` check exists to prevent, and it fails OPEN.
    Without the check, any meeting one other invitee declined disappears
    from the busy list, and the principal is offered a slot they are
    already in a meeting for.
    """
    someone_else_declined = BUSY | {"attendees": [
        {"email": "me@example.com", "self": True,
         "responseStatus": "accepted"},
        {"email": "bob@example.com", "responseStatus": "declined"}]}

    blocks = connector([someone_else_declined]).busy_blocks(DAY, TZ)

    assert len(blocks) == 1, "only the PRINCIPAL's decline frees a slot"


def test_an_event_marked_free_is_not_busy():
    """Transparent means on the calendar, deliberately not blocking."""
    assert connector([BUSY | {"transparency": "transparent"}]) \
        .busy_blocks(DAY, TZ) == []


def test_an_event_marked_busy_explicitly_is_still_busy():
    """`transparency: opaque` is the default, spelled out. A filter that
    tested truthiness instead of the literal "transparent" would drop it.
    """
    blocks = connector([BUSY | {"transparency": "opaque"}]).busy_blocks(DAY, TZ)

    assert len(blocks) == 1


def test_a_cancelled_event_is_not_busy():
    assert connector([BUSY | {"status": "cancelled"}]) \
        .busy_blocks(DAY, TZ) == []


def test_a_confirmed_event_is_busy():
    """Paired with the test above so `status` is checked in both
    directions, not only the rejecting one.
    """
    blocks = connector([BUSY | {"status": "confirmed"}]).busy_blocks(DAY, TZ)

    assert len(blocks) == 1


# --- 5. the query: the half of the contract the result cannot show ---------

def test_recurrence_is_expanded_by_the_server():
    """Without singleEvents a weekly standup comes back ONCE, with a
    recurrence rule, and the agent reads the principal as free every week
    but the first. Nothing in the returned blocks shows this, which is
    why it is asserted on the request.
    """
    cal = connector([BUSY])
    cal.busy_blocks(DAY, TZ)

    assert cal._service.calls[0]["singleEvents"] is True


def test_the_window_is_one_local_day_in_the_callers_timezone():
    """Midnight to midnight in the PRINCIPAL's timezone, not UTC. A UTC
    window would shift the day by 5h30: it would miss a late evening
    meeting and import one from the day before.
    """
    cal = connector([BUSY])
    cal.busy_blocks(DAY, TZ)
    call = cal._service.calls[0]

    assert call["timeMin"] == "2026-08-27T00:00:00+05:30"
    assert call["timeMax"] == "2026-08-28T00:00:00+05:30"
    assert call["timeZone"] == "Asia/Kolkata"


# --- 6. multiple calendars ------------------------------------------------

def test_every_configured_calendar_is_queried_and_blocks_are_tagged():
    """A conflict on the work calendar is as real as one on the personal
    calendar, and the tag is how the digest can say which.
    """
    cal = connector(
        {"primary": [BUSY],
         "work@example.com": [timed("1:1", "2026-08-27T11:00:00+05:30",
                                    "2026-08-27T11:30:00+05:30")]},
        calendar_ids=("primary", "work@example.com"))

    blocks = cal.busy_blocks(DAY, TZ)

    assert [c["calendarId"] for c in cal._service.calls] == \
        ["primary", "work@example.com"]
    assert {b["calendar"] for b in blocks} == {"primary", "work@example.com"}


def test_blocks_from_all_calendars_are_merged_in_time_order():
    """Each calendar is ordered by the server, but joining two ordered
    lists does not give an ordered list. The agent reads this list top
    down when it reports the day, so the merge must sort again.
    """
    cal = connector(
        {"primary": [timed("Late", "2026-08-27T17:00:00+05:30",
                           "2026-08-27T18:00:00+05:30")],
         "work@example.com": [
             all_day("Public holiday", "2026-08-27", "2026-08-28"),
             timed("Early", "2026-08-27T09:00:00+05:30",
                   "2026-08-27T09:30:00+05:30")]},
        calendar_ids=("primary", "work@example.com"))

    titles = [b["title"] for b in cal.busy_blocks(DAY, TZ)]

    # All-day first: it frames the whole day, so it belongs above the
    # clock times it applies to.
    assert titles == ["Public holiday", "Early", "Late"]
