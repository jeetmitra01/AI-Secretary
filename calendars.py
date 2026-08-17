"""
Phase 2: read-only calendar, behind a connector interface like ADR-002's.

`check_calendar` has been a stub returning [] since the first agent loop.
This is the real thing. It reads; it can never write — `calendar.readonly`
is the only scope requested, so ADR-003's "capability restriction, not
prompt wording" still holds at the auth layer, one capability wider.

Setup (one-off, in the SAME Google Cloud project as Gmail):
    1. Enable the Google Calendar API
    2. Add .../auth/calendar.readonly to the OAuth consent screen scopes
    3. python calendars.py     # re-consents, KEEPING gmail.readonly

Smoke test:
    python calendars.py        # prints today's busy blocks
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from connectors import ReauthorizationRequired, load_credentials  # noqa: F401

CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]


class CalendarConnector(ABC):
    """What every calendar provider must offer. Contract for callers:

    - busy_blocks answers for ONE day, in the timezone the caller passes.
      The caller owns the timezone because "what day is it" is a question
      about the principal, not about the calendar server.
    - recurring events are expanded to concrete instances
    - events the principal DECLINED are not busy
    - events marked Free (transparent) are not busy
    - all-day events come back with all_day=True and no clock times, since
      "busy 00:00-23:59" would be a lie about an all-day 'Vacation'
    - an empty list means free, and is never used to mean "lookup failed"
    """

    @abstractmethod
    def busy_blocks(self, day: date, tz: ZoneInfo) -> list[dict]:
        ...


class GoogleCalendarConnector(CalendarConnector):
    source = "google"

    def __init__(self, credentials_file: str = "credentials.json",
                 token_file: str = "token.json",
                 allow_interactive_auth: bool = False,
                 calendar_ids: tuple[str, ...] = ("primary",)):
        self.credentials_file = credentials_file
        # Same token file as Gmail on purpose: one Google account, one
        # consent, one 7-day Testing expiry to re-do instead of two.
        # load_credentials widens the grant without dropping gmail.readonly.
        self.token_file = token_file
        self.allow_interactive_auth = allow_interactive_auth
        self.calendar_ids = calendar_ids
        self._service = None

    def _get_service(self):
        if self._service is not None:
            return self._service
        from googleapiclient.discovery import build
        creds = load_credentials(CALENDAR_SCOPES, self.credentials_file,
                                 self.token_file, self.allow_interactive_auth)
        self._service = build("calendar", "v3", credentials=creds)
        return self._service

    def busy_blocks(self, day: date, tz: ZoneInfo) -> list[dict]:
        service = self._get_service()
        start = datetime.combine(day, time.min, tzinfo=tz)
        end = start + timedelta(days=1)

        blocks: list[dict] = []
        for cal_id in self.calendar_ids:
            resp = service.events().list(
                calendarId=cal_id,
                timeMin=start.isoformat(), timeMax=end.isoformat(),
                # Expand recurrence server-side. Without this a weekly
                # standup returns as ONE event with a recurrence rule, and
                # the agent concludes you are free every week but the first.
                singleEvents=True,
                orderBy="startTime",
                timeZone=str(tz),
            ).execute()

            for ev in resp.get("items", []):
                if ev.get("status") == "cancelled":
                    continue
                if ev.get("transparency") == "transparent":
                    continue          # marked Free — on the calendar, not busy
                if _declined(ev):
                    continue          # you said no; that slot is open

                blocks.append(_to_block(ev, tz, cal_id))

        blocks.sort(key=lambda b: (b["all_day"] is False, b.get("start") or ""))
        return blocks


def _declined(ev: dict) -> bool:
    """True if the principal declined. `self` marks which attendee is us —
    without that check we would skip events any OTHER guest declined."""
    for att in ev.get("attendees", []):
        if att.get("self") and att.get("responseStatus") == "declined":
            return True
    return False


def _to_block(ev: dict, tz: ZoneInfo, cal_id: str) -> dict:
    title = ev.get("summary", "(no title)")
    start, end = ev.get("start", {}), ev.get("end", {})

    # All-day events carry `date`; timed events carry `dateTime`. Reading
    # the wrong one is a KeyError on exactly the events people forget to
    # test with (birthdays, PTO, public holidays).
    if "date" in start:
        return {"title": title, "all_day": True,
                "date": start["date"], "calendar": cal_id}

    return {
        "title": title,
        "all_day": False,
        "start": datetime.fromisoformat(start["dateTime"])
                         .astimezone(tz).strftime("%H:%M"),
        "end": datetime.fromisoformat(end["dateTime"])
                       .astimezone(tz).strftime("%H:%M"),
        "calendar": cal_id,
    }


CALENDARS = {"google": GoogleCalendarConnector}


if __name__ == "__main__":
    # The one entrypoint where a human is definitionally present, so this
    # is also how you consent to the calendar scope the first time.
    import sys

    from console import use_utf8
    use_utf8()          # event titles are arbitrary Unicode (console.py)

    tz = ZoneInfo("Asia/Kolkata")
    day = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 \
        else datetime.now(tz).date()

    cal = GoogleCalendarConnector(allow_interactive_auth=True)
    blocks = cal.busy_blocks(day, tz)
    print(f"{day} ({tz.key}): {len(blocks)} busy block(s)")
    for b in blocks:
        when = "all day" if b["all_day"] else f"{b['start']}-{b['end']}"
        print(f"  {when:>13}  {b['title']}")
