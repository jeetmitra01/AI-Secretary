"""
Email connectors: one interface, provider-specific implementations.

Setup (Gmail):
    pip install google-api-python-client google-auth-oauthlib
    1. console.cloud.google.com -> new project -> enable "Gmail API"
    2. OAuth consent screen -> External -> Testing -> add yourself as test user
    3. Credentials -> Create OAuth client ID -> "Desktop app"
    4. Download the JSON as credentials.json next to this file
    First run opens a browser to authorize; token is cached in token.json.

    NOTE: while the consent screen sits in "Testing", Google expires the
    refresh token after 7 DAYS. Re-authorizing weekly is the cost of not
    publishing; see ReauthorizationRequired below for how that surfaces.

Smoke test:
    python connectors.py          # prints your last 24h of email, normalized
"""

from __future__ import annotations

import base64
import re
import sys
from abc import ABC, abstractmethod
from collections.abc import Collection
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

from models import Email

# READ-ONLY on purpose. Widening this scope later (e.g. to create drafts)
# is a deliberate security decision, not a config tweak. If you change
# scopes, delete token.json and re-authorize.
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Sort floor for emails with no received_at — they sort last (ADR-011)
# rather than blowing up the comparison against tz-aware datetimes.
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class ReauthorizationRequired(Exception):
    """The cached token is dead and renewing it needs a human at a browser.

    Raised INSTEAD of opening a browser when nobody is watching. A
    scheduled run that calls run_local_server() blocks on a consent page
    no one will ever see, until Task Scheduler's ExecutionTimeLimit kills
    it 15 minutes later — a hang that looks identical to a slow run.
    Failing fast and loudly is recoverable; hanging silently is not.
    """


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------

class EmailConnector(ABC):
    """What every provider must offer. Contract expected by callers:
    - INBOUND mail only: messages the principal sent, and unsent drafts,
      are the principal's own words and never appear in a digest
    - bodies are plain text (no HTML)
    - timestamps are timezone-aware UTC
    - fetch_since may return duplicates across overlapping calls;
      callers dedupe by Email.id (see the watermark note below)
    - fetch_since returns newest received_at FIRST (ADR-011). Emails
      with no received_at sort last. This is a presentation guarantee
      about the emails you got — it says NOTHING about the ones you
      didn't, so never read coverage into it
    - fetch_since silently returns only max_results matches; pair it
      with count_since to detect that truncation
    - both take the same skip_ids, so both answer about the SAME
      population. Passing it to one and not the other silently compares
      two different sets (ADR-015)
    """

    source: str = "abstract"

    @abstractmethod
    def fetch_since(self, dt: datetime, max_results: int = 50,
                    skip_ids: Collection[str] = ()) -> list[Email]:
        """Newest max_results emails in the window, skipping skip_ids.

        skip_ids is applied while LISTING, before any body is fetched —
        that is the whole point (ADR-015). Filtering after the fact would
        keep handing back the same newest max_results forever, so a
        backlog could never drain."""
        ...

    @abstractmethod
    def count_since(self, dt: datetime, skip_ids: Collection[str] = ()) -> int:
        """How many emails fall in the window, ignoring max_results.
        Cheap relative to fetch_since — it lists ids and never fetches
        bodies. Callers compare it against what fetch_since returned to
        tell whether they are looking at the whole window or a slice."""
        ...


# ---------------------------------------------------------------------------
# Gmail
# ---------------------------------------------------------------------------

class GmailConnector(EmailConnector):
    source = "gmail"

    def __init__(self, credentials_file: str = "credentials.json",
                 token_file: str = "token.json",
                 allow_interactive_auth: bool = False):
        self.credentials_file = credentials_file
        self.token_file = token_file
        # Opt-in, and defaults to OFF. The tempting version of this is to
        # sniff for a human with sys.stdin.isatty() — that was tried and
        # it lies: it returned True in a console-less process, which then
        # opened a browser nobody could see and blocked on the OAuth
        # redirect server until the run was killed. Exactly the hang this
        # class exists to prevent. Callers know whether a human is there;
        # the process does not. Make them say so.
        self.allow_interactive_auth = allow_interactive_auth
        self._service = None  # built lazily on first fetch

    # -- auth ---------------------------------------------------------------

    def _get_service(self):
        if self._service is not None:
            return self._service

        # Imports live here so the module loads even before deps install.
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = None
        if Path(self.token_file).exists():
            creds = Credentials.from_authorized_user_file(
                self.token_file, GMAIL_SCOPES)

        if not creds or not creds.valid:
            creds = self._renew(creds)
            Path(self.token_file).write_text(creds.to_json())

        self._service = build("gmail", "v1", credentials=creds)
        return self._service

    def _renew(self, creds):
        """Get back to valid credentials, by the cheapest route available.

        Three outcomes, and the old code only handled the happy one:
        refresh succeeds (silent, the common case); refresh is rejected
        because the refresh token itself is dead (7-day Testing expiry,
        a manual revoke, a password change); or there is nothing to
        refresh from. The last two both mean "a human must re-consent".
        """
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        from google_auth_oauthlib.flow import InstalledAppFlow

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

        if not self.allow_interactive_auth:
            raise ReauthorizationRequired(
                f"{why}. Gmail needs consent again and this process has no "
                f"console to prompt at. Run `python connectors.py` in a "
                f"terminal to re-authorize, then the schedule resumes."
            )

        print(f"[auth] {why} — opening browser to re-authorize.",
              file=sys.stderr)
        flow = InstalledAppFlow.from_client_secrets_file(
            self.credentials_file, GMAIL_SCOPES)
        return flow.run_local_server(port=0)

    # -- fetching -----------------------------------------------------------

    @staticmethod
    def _query(dt: datetime) -> str:
        # Gmail's `after:` filter takes epoch SECONDS and is coarse; we
        # already overlap our windows (see watermark note at bottom), so
        # coarseness is fine — dedupe handles the rest.
        #
        # A bare Gmail search covers ALL MAIL, not the inbox, so your own
        # sent messages come back and get summarized at you. Excluded by
        # LABEL rather than by `-from:me`: from:me also matches envelope /
        # Return-Path addresses, and bulk senders use VERP bounce paths
        # with your address embedded in them —
        #   Return-Path: <bounce+afde51-jeetmitra01=gmail.com@substack.com>
        # — so `-from:me` measurably drops real inbound newsletters
        # (5 of them over a 7-day window when this was tested). Replies to
        # mail you sent are unaffected either way: they are separate
        # messages, labelled INBOX, with the other party in From:.
        return f"after:{int(dt.timestamp())} -in:sent -in:draft -in:chats"

    def count_since(self, dt: datetime, skip_ids: Collection[str] = ()) -> int:
        service = self._get_service()
        query = self._query(dt)
        skip = frozenset(skip_ids)

        # Counting ids, not trusting resultSizeEstimate — that field is an
        # estimate and drifts from the real count on large mailboxes. One
        # list call per 500 ids, and no messages().get() at all, so this
        # stays cheap next to fetch_since's per-message round trips.
        total, page_token = 0, None
        while True:
            resp = service.users().messages().list(
                userId="me", q=query, pageToken=page_token, maxResults=500,
            ).execute()
            total += sum(1 for m in resp.get("messages", [])
                         if m["id"] not in skip)
            page_token = resp.get("nextPageToken")
            if not page_token:
                return total

    def fetch_since(self, dt: datetime, max_results: int = 50,
                    skip_ids: Collection[str] = ()) -> list[Email]:
        service = self._get_service()
        query = self._query(dt)
        skip = frozenset(skip_ids)

        # Page at the API maximum rather than sizing pages to the quota:
        # most of a page can be skip_ids, and listing is ids-only, so an
        # extra page is far cheaper than an extra round of messages().get.
        ids: list[str] = []
        page_token = None
        while len(ids) < max_results:
            resp = service.users().messages().list(
                userId="me", q=query, pageToken=page_token, maxResults=500,
            ).execute()
            for m in resp.get("messages", []):
                if m["id"] not in skip:
                    ids.append(m["id"])
                    if len(ids) >= max_results:
                        break
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        emails = []
        for msg_id in ids:
            raw = service.users().messages().get(
                userId="me", id=msg_id, format="full").execute()
            emails.append(self._normalize(raw))

        # ADR-011: Gmail's list order follows the sender-supplied `Date:`
        # header, which ADR-008 already refused to trust — observed ~8h
        # out of order. Sort on internalDate, the value we do trust.
        #
        # Note what this cannot do: the slice above was already taken in
        # Gmail's order, so sorting fixes the sequence of what we got,
        # never the selection of what we cut. Coverage is count_since's
        # job, not this sort's.
        return sorted(emails, key=lambda e: (e.received_at is not None,
                                             e.received_at or _EPOCH),
                      reverse=True)

    # -- normalization: where provider chaos becomes the Email contract ----

    def _normalize(self, raw: dict) -> Email:
        payload = raw.get("payload", {})
        headers = {h["name"].lower(): h["value"]
                   for h in payload.get("headers", [])}

        # internalDate = ms since epoch, Gmail's authoritative receipt time.
        # We use it instead of parsing the Date: header (senders lie/vary).
        received = datetime.fromtimestamp(
            int(raw.get("internalDate", 0)) / 1000, tz=timezone.utc)

        return Email(
            id=raw["id"],
            thread_id=raw.get("threadId", raw["id"]),
            sender=headers.get("from", ""),
            recipients=[a.strip() for a in headers.get("to", "").split(",")
                        if a.strip()],
            subject=headers.get("subject", ""),
            body_text=self._extract_body(payload),
            received_at=received,
            source=self.source,
        )

    def _extract_body(self, payload: dict) -> str:
        """Gmail bodies are a MIME tree: multipart containers holding
        text/plain and/or text/html leaves, possibly nested (forwards,
        signatures, attachments). Strategy: walk the tree, prefer
        text/plain, fall back to stripped text/html."""
        plain, html = self._walk_parts(payload)
        if plain:
            return plain.strip()
        if html:
            return _strip_html(html).strip()
        return ""

    def _walk_parts(self, part: dict) -> tuple[str, str]:
        mime = part.get("mimeType", "")
        children = part.get("parts", [])

        # multipart/alternative holds competing renderings of the SAME
        # content. Concatenating duplicates the email; pick the richest
        # candidate of each kind instead.
        if mime == "multipart/alternative" and children:
            plain, html = "", ""
            for child in children:
                p, h = self._walk_parts(child)
                plain = max(plain, p, key=len)
                html = max(html, h, key=len)
            return plain, html

        plain, html = "", ""
        data = part.get("body", {}).get("data")

        if data:  # a leaf with content
            text = _b64url_decode(data)
            # Trust content over label: ESPs routinely dump full HTML into
            # the text/plain slot, which sails past the prefer-plain rule
            # in _extract_body and reaches callers as raw markup.
            if mime == "text/plain" and not _looks_like_html(text):
                plain += text
            elif mime in ("text/plain", "text/html"):
                html += text

        for child in children:  # mixed/related: genuine parts, concatenate
            p, h = self._walk_parts(child)
            plain += p
            html += h
        return plain, html


def _looks_like_html(text: str) -> bool:
    """Catches wholesale mislabeling (a full HTML document parked in a
    text/plain part). A plain part with tags only sprinkled mid-body still
    slips through; that needs tag-density heuristics and their false
    positives."""
    return text.lstrip()[:200].lower().startswith(("<!doctype", "<html"))


def _b64url_decode(data: str) -> str:
    # Gmail uses URL-safe base64, sometimes without padding.
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")


class _HTMLToText(HTMLParser):
    """Minimal, dependency-free HTML -> text. Good enough for email bodies;
    swap for BeautifulSoup later if you hit gnarly newsletters."""
    SKIP = {"style", "script", "head"}

    def __init__(self):
        super().__init__()
        self.chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip_depth += 1
        elif tag in ("br", "p", "div", "tr", "li"):
            self.chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if not self._skip_depth:
            self.chunks.append(data)


def _strip_html(html: str) -> str:
    p = _HTMLToText()
    p.feed(html)
    text = "".join(p.chunks)
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", text))


# ---------------------------------------------------------------------------
# Outlook — stub behind the same contract, filled in when you point an
# agent instance at an Outlook account. Uses Microsoft Graph + MSAL.
# ---------------------------------------------------------------------------

class OutlookConnector(EmailConnector):
    source = "outlook"

    def fetch_since(self, dt: datetime, max_results: int = 50,
                    skip_ids: Collection[str] = ()) -> list[Email]:
        raise NotImplementedError(
            "Outlook: register an app in Azure Portal (Entra ID), scope "
            "Mail.Read, auth via msal device-code flow, then GET "
            "/me/messages?$filter=receivedDateTime ge {iso} and normalize "
            "into the same Email object. Order by receivedDateTime desc "
            "(ADR-011) and drop skip_ids while paging, before fetching "
            "bodies (ADR-015)."
        )

    # Defined rather than omitted on purpose: leaving it out would make
    # __abstractmethods__ non-empty and OutlookConnector un-instantiable,
    # so CONNECTORS["outlook"]() would die with a generic TypeError at
    # construction instead of this message at the point of real use.
    def count_since(self, dt: datetime, skip_ids: Collection[str] = ()) -> int:
        raise NotImplementedError(
            "Outlook: same $filter as fetch_since, but request "
            "$count=true with the ConsistencyLevel: eventual header and "
            "read @odata.count instead of paging the messages. Note "
            "$count cannot subtract skip_ids, so a non-empty skip_ids "
            "means paging ids and filtering, as Gmail does."
        )


CONNECTORS = {"gmail": GmailConnector, "outlook": OutlookConnector}


# ---------------------------------------------------------------------------
# Smoke test + the watermark pattern your scheduler will use:
#   next_fetch_from = last_run_time - overlap (e.g. 5 min), dedupe by id.
#   Overlap guarantees no gaps; dedupe by Email.id kills the duplicates
#   the overlap creates. Gaps lose data silently; duplicates are cheap.
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # The one entrypoint where a human is definitionally present, so this
    # is also how you re-consent after a token dies: python connectors.py
    connector = GmailConnector(allow_interactive_auth=True)
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    emails = connector.fetch_since(since, max_results=20)

    seen: set[str] = set()          # the dedupe half of the watermark pattern
    for e in emails:
        if e.id in seen:
            continue
        seen.add(e.id)
        print(e.brief(body_chars=120))
        print()
    print(f"{len(seen)} unique emails in the last 24h.")
