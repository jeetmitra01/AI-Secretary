"""
Google OAuth: the token dance, and the list of grants it mints.

Every Google capability this project has — `gmail.readonly` in
connectors.py, `calendar.readonly` in calendars.py, `calendar.events` on
its own token in executor.py — needs the identical dance: load a cached
token, check it actually covers the scopes asked for, refresh it if that
is enough, and otherwise stop with a sentence a human can act on. That
dance is a property of Google OAuth, not of mail or of calendars, so it
lives in one file that knows about neither.

    python auth.py            consent to everything the service needs
    python auth.py --check    what is granted right now; opens no browser

ONE command, TWO grants, TWO token files (ADR-027). That is not a detail
to tidy up later — it is the whole security model:

    token.json         gmail.readonly + calendar.readonly
                       Held by the scheduled digest and by /chat, which
                       read untrusted email bodies. Cannot write.
    token_write.json   calendar.events
                       Held ONLY by executor.py, which never reads mail
                       and is not in any tool registry (ADR-023).

Merging those two files would hand a write capability to the code path
that reads attacker-controlled text. Nothing in this file may do that.
See `GRANTS` below, which is where a new capability gets added.

The scope CONSTANTS still live with their callers (ADR-026): GMAIL_SCOPES
in connectors.py, CALENDAR_SCOPES in calendars.py, CALENDAR_WRITE_SCOPES
in executor.py. `GRANTS` references them; it does not restate them. A
scope belongs beside the code that uses the capability, and this file
only decides which TOKEN each one lands on.
"""

from __future__ import annotations

import sys
from collections.abc import Collection
from dataclasses import dataclass
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
            f"console to prompt at. Run `python auth.py` in a terminal to "
            f"re-authorize, then run this again."
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


# ---------------------------------------------------------------------------
# The grants (ADR-027) — what `python auth.py` asks Google for
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Grant:
    """One token file, and every scope that may share it.

    `proves` is not decoration. ADR-026 argued against a generic consent
    command precisely because minting a token tells you nothing about
    whether the capability works — `python calendars.py` re-consents AND
    prints today's busy blocks. Naming the smoke test here keeps that
    check one command away instead of losing it.
    """
    name: str
    token_file: str
    scopes: tuple[str, ...]
    proves: str
    why_separate: str = ""


def GRANTS() -> list[Grant]:
    """Every capability this service needs, grouped by token file.

    ==> THIS IS WHERE A NEW CAPABILITY GETS ADDED. <==

    Three steps, in this order:

      1. Declare the scope constant NEXT TO THE CODE THAT USES IT, not
         here (ADR-026). This function references constants; it does not
         own them.
      2. Decide which token file it belongs on, and that decision is a
         security decision, not a convenience one. The rule:

             Does the scope let the model's code path CHANGE anything in
             the world? Then it does NOT go on `token.json`.

         `token.json` is held by the scheduled digest and by /chat, both
         of which put untrusted email bodies into a model's context
         (ADR-004). A write scope there is reachable by anyone who can
         send you mail. Give it its own file and its own zone, the way
         ADR-023 did for calendar.events.
      3. Add the line below, with a `proves` command that EXERCISES the
         new capability rather than merely minting the token.

    Email drafts are the next capability due (`gmail.compose`). By the
    rule above it is a write, so it belongs on token_write.json or on a
    third file — never on token.json.

    Imported lazily because connectors.py, calendars.py and executor.py
    all import THIS module at their top. At module level these imports
    would be a cycle; inside a function they run after auth.py is fully
    loaded, which is always.
    """
    from calendars import CALENDAR_SCOPES
    from connectors import GMAIL_SCOPES
    from executor import CALENDAR_WRITE_SCOPES, WRITE_TOKEN_FILE

    return [
        Grant(
            name="read",
            token_file="token.json",
            scopes=(*GMAIL_SCOPES, *CALENDAR_SCOPES),
            proves="python connectors.py   /   python calendars.py",
        ),
        Grant(
            name="write",
            token_file=WRITE_TOKEN_FILE,
            scopes=tuple(CALENDAR_WRITE_SCOPES),
            proves="python executor.py",
            why_separate=(
                "the unattended paths (scheduled digest, /chat) read "
                "untrusted email and must never hold a credential that "
                "writes (ADR-023)"),
        ),
    ]


def granted_scopes(token_file: str) -> list[str] | None:
    """What `token_file` actually holds, or None if there is no token.

    Reads the file directly instead of going through load_credentials:
    this must never refresh, never consent, and never raise — it is the
    reporting path, and it is used to decide whether to prompt at all.
    """
    from google.oauth2.credentials import Credentials

    if not Path(token_file).exists():
        return None
    try:
        return sorted(Credentials.from_authorized_user_file(
            token_file).scopes or [])
    except (ValueError, KeyError):          # truncated / hand-edited file
        return []


def _report(grant: Grant) -> bool:
    """Print what this grant has versus what it needs. True if satisfied."""
    have = granted_scopes(grant.token_file)
    missing = set(grant.scopes) - set(have or [])

    if have is None:
        print(f"  {grant.token_file:<18} MISSING — no token file yet")
    elif missing:
        print(f"  {grant.token_file:<18} INCOMPLETE")
        for scope in sorted(missing):
            print(f"{'':<20}   needs {scope}")
    else:
        print(f"  {grant.token_file:<18} ok — {len(have)} scope(s)")
        for scope in have:
            print(f"{'':<20}   {scope}")
    return not missing and have is not None


if __name__ == "__main__":
    from console import use_utf8
    use_utf8()          # Google's error strings and account names are
                        # arbitrary Unicode (ADR-021)

    grants = GRANTS()
    check_only = "--check" in sys.argv[1:]

    print("Grants this service needs:\n")
    satisfied = [_report(g) for g in grants]

    if check_only:
        print("\n--check: nothing was requested. Drop the flag to consent.")
        sys.exit(0 if all(satisfied) else 1)

    print()
    for grant, ok in zip(grants, satisfied):
        if ok:
            # Already covered, so no browser. Re-consenting a good token
            # buys nothing and costs you a round of clicking.
            print(f"[{grant.name}] {grant.token_file}: already covered, "
                  f"skipping")
            continue

        if grant.why_separate:
            print(f"[{grant.name}] separate token, because "
                  f"{grant.why_separate}")
        print(f"[{grant.name}] requesting {len(grant.scopes)} scope(s) "
              f"-> {grant.token_file}")
        # One flow per token file, never one flow for everything: the
        # whole point is that these land in different files. Each opens
        # its own browser window, and that visible repetition is the
        # separation being enforced rather than described.
        load_credentials(grant.scopes, token_file=grant.token_file,
                         allow_interactive_auth=True)

    print("\nDone. Prove each capability works:")
    for grant in grants:
        print(f"  {grant.name:<6} {grant.proves}")
