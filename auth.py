"""
Google OAuth, and nothing else (ADR-026).

Every Google capability this project has — `gmail.readonly` in
connectors.py, `calendar.readonly` in calendars.py, `calendar.events` on
its own token in executor.py — needs the identical dance: load a cached
token, check it actually covers the scopes asked for, refresh it if that
is enough, and otherwise stop with a sentence a human can act on. That
dance is a property of Google OAuth, not of mail or of calendars, so it
lives in one file that knows about neither.

What this file does NOT hold, on purpose:

    the scope constants     Each caller declares what IT needs
                            (GMAIL_SCOPES, CALENDAR_SCOPES,
                            CALENDAR_WRITE_SCOPES). A scope is a
                            statement about a capability, and it belongs
                            next to the code that uses the capability —
                            not in a list that invites "while I'm here".
    a __main__             There is no `python auth.py`. Re-consent
                            happens through the entrypoint that needs the
                            scope, because that entrypoint is what proves
                            the new grant works:
                                python connectors.py       gmail.readonly
                                python calendars.py        calendar.readonly
                                python executor.py --consent
                                                           calendar.events

The two-zone split (ADR-023) survives this move. `token_write.json` is a
separate FILE and `calendar.events` a separate grant; this module reads
whichever token file it is handed and adds no capability to either.
"""

from __future__ import annotations

import sys
from collections.abc import Collection
from pathlib import Path


class ReauthorizationRequired(Exception):
    """The cached token is dead and renewing it needs a human at a browser.

    Raised INSTEAD of opening a browser when nobody is watching. A
    scheduled run that calls run_local_server() blocks on a consent page
    no one will ever see, until Task Scheduler's ExecutionTimeLimit kills
    it 15 minutes later — a hang that looks identical to a slow run.
    Failing fast and loudly is recoverable; hanging silently is not.
    """


def load_credentials(scopes: Collection[str],
                     credentials_file: str = "credentials.json",
                     token_file: str = "token.json",
                     allow_interactive_auth: bool = False):
    """Valid credentials covering `scopes`, or a clear reason why not."""
    from google.oauth2.credentials import Credentials

    scopes = list(scopes)
    creds = None
    if Path(token_file).exists():
        # NO scopes argument here, and that is load-bearing. Passing scopes
        # makes Credentials report the scopes you ASKED FOR as though they
        # were granted, overwriting what the file actually says. The
        # sufficiency check below then compares a set against itself, is
        # always empty, and silently never fires. Omitting the argument
        # leaves creds.scopes as the real grant.
        creds = Credentials.from_authorized_user_file(token_file)

    # Scope sufficiency is NOT part of creds.valid, and that gap is a trap:
    # a token minted for gmail.readonly stays "valid" forever while every
    # Calendar call returns 403 "Request had insufficient authentication
    # scopes" — which reads like a broken permission grant rather than
    # "consent again for the new scope". Checking here names the real cure
    # at the point of failure instead of 403-ing deep inside a tool call,
    # and skips a refresh round trip that could not have helped.
    missing = set(scopes) - set(creds.scopes or []) if creds else set(scopes)

    if creds and creds.valid and not missing:
        return creds

    creds = _renew(creds, scopes, credentials_file, allow_interactive_auth,
                   missing)
    Path(token_file).write_text(creds.to_json())
    return creds


def _renew(creds, scopes, credentials_file, allow_interactive_auth, missing):
    """Get back to valid credentials, by the cheapest route available.

    Four outcomes now, and the original code only handled the happy one:
    refresh succeeds (silent, the common case); the token is missing a
    scope, which refreshing can NEVER add; refresh is rejected because the
    refresh token itself is dead (7-day Testing expiry, a manual revoke, a
    password change); or there is nothing to refresh from. All but the
    first mean "a human must re-consent".
    """
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow

    if missing:
        # Deliberately before the refresh attempt: a refresh returns the
        # scopes already granted, so refreshing to obtain a new one just
        # burns a round trip and then fails identically.
        why = f"token lacks {', '.join(sorted(missing))}"
    else:
        why = "no cached token"
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())          # silent refresh
                return creds
            except RefreshError as e:
                # Google says invalid_grant for expired AND revoked alike;
                # the response body doesn't distinguish them, so neither
                # can we. Either way the only cure is a fresh consent.
                why = f"refresh rejected ({e.args[0] if e.args else e})"

    if not allow_interactive_auth:
        raise ReauthorizationRequired(
            f"{why}. Google needs consent again and this process has no "
            f"console to prompt at. Run `python connectors.py` in a "
            f"terminal to re-authorize, then the schedule resumes."
        )

    # Consent for the UNION of what we need and what the token already had.
    # Requesting only `scopes` would silently narrow the grant: adding
    # calendar.readonly to a gmail.readonly token would come back holding
    # calendar alone, and the next digest run would fail authorization on a
    # mailbox that worked ten seconds earlier. Re-consent must never take a
    # capability away as a side effect of adding one.
    granted = set(creds.scopes or []) if creds else set()
    request = sorted(set(scopes) | granted)
    if granted - set(scopes):
        print(f"[auth] preserving existing scope(s): "
              f"{', '.join(sorted(granted - set(scopes)))}", file=sys.stderr)

    print(f"[auth] {why} — opening browser to re-authorize.", file=sys.stderr)
    flow = InstalledAppFlow.from_client_secrets_file(credentials_file, request)
    return flow.run_local_server(port=0)
