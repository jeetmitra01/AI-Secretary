"""
Email connectors: one interface, provider-specific implementations.

Setup (Gmail):
    pip install google-api-python-client google-auth-oauthlib
    1. console.cloud.google.com -> new project -> enable "Gmail API"
    2. OAuth consent screen -> External -> Testing -> add yourself as test user
    3. Credentials -> Create OAuth client ID -> "Desktop app"
    4. Download the JSON as credentials.json next to this file
    First run opens a browser to authorize; token is cached in token.json.

Smoke test:
    python connectors.py          # prints your last 24h of email, normalized
"""

from __future__ import annotations

import base64
import re
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

from models import Email

# READ-ONLY on purpose. Widening this scope later (e.g. to create drafts)
# is a deliberate security decision, not a config tweak. If you change
# scopes, delete token.json and re-authorize.
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------

class EmailConnector(ABC):
    """What every provider must offer. Contract expected by callers:
    - bodies are plain text (no HTML)
    - timestamps are timezone-aware UTC
    - fetch_since may return duplicates across overlapping calls;
      callers dedupe by Email.id (see the watermark note below)
    - fetch_since silently returns only the newest max_results matches;
      pair it with count_since to detect that truncation
    """

    source: str = "abstract"

    @abstractmethod
    def fetch_since(self, dt: datetime, max_results: int = 50) -> list[Email]:
        ...

    @abstractmethod
    def count_since(self, dt: datetime) -> int:
        """How many emails actually fall in the window, ignoring
        max_results. Cheap relative to fetch_since — it lists ids and
        never fetches bodies. Callers compare it against what
        fetch_since returned to tell whether they are looking at the
        whole window or the newest slice of it."""
        ...


# ---------------------------------------------------------------------------
# Gmail
# ---------------------------------------------------------------------------

class GmailConnector(EmailConnector):
    source = "gmail"

    def __init__(self, credentials_file: str = "credentials.json",
                 token_file: str = "token.json"):
        self.credentials_file = credentials_file
        self.token_file = token_file
        self._service = None  # built lazily on first fetch

    # -- auth ---------------------------------------------------------------

    def _get_service(self):
        if self._service is not None:
            return self._service

        # Imports live here so the module loads even before deps install.
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build

        creds = None
        if Path(self.token_file).exists():
            creds = Credentials.from_authorized_user_file(
                self.token_file, GMAIL_SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())          # silent refresh
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.credentials_file, GMAIL_SCOPES)
                creds = flow.run_local_server(port=0)  # opens browser once
            Path(self.token_file).write_text(creds.to_json())

        self._service = build("gmail", "v1", credentials=creds)
        return self._service

    # -- fetching -----------------------------------------------------------

    @staticmethod
    def _query(dt: datetime) -> str:
        # Gmail's `after:` filter takes epoch SECONDS and is coarse; we
        # already overlap our windows (see watermark note at bottom), so
        # coarseness is fine — dedupe handles the rest.
        return f"after:{int(dt.timestamp())}"

    def count_since(self, dt: datetime) -> int:
        service = self._get_service()
        query = self._query(dt)

        # Counting ids, not trusting resultSizeEstimate — that field is an
        # estimate and drifts from the real count on large mailboxes. One
        # list call per 500 ids, and no messages().get() at all, so this
        # stays cheap next to fetch_since's per-message round trips.
        total, page_token = 0, None
        while True:
            resp = service.users().messages().list(
                userId="me", q=query, pageToken=page_token, maxResults=500,
            ).execute()
            total += len(resp.get("messages", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                return total

    def fetch_since(self, dt: datetime, max_results: int = 50) -> list[Email]:
        service = self._get_service()
        query = self._query(dt)

        ids: list[str] = []
        page_token = None
        while len(ids) < max_results:
            resp = service.users().messages().list(
                userId="me", q=query, pageToken=page_token,
                maxResults=min(max_results - len(ids), 100),
            ).execute()
            ids += [m["id"] for m in resp.get("messages", [])]
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        emails = []
        for msg_id in ids:
            raw = service.users().messages().get(
                userId="me", id=msg_id, format="full").execute()
            emails.append(self._normalize(raw))
        return emails

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

    def fetch_since(self, dt: datetime, max_results: int = 50) -> list[Email]:
        raise NotImplementedError(
            "Outlook: register an app in Azure Portal (Entra ID), scope "
            "Mail.Read, auth via msal device-code flow, then GET "
            "/me/messages?$filter=receivedDateTime ge {iso} and normalize "
            "into the same Email object."
        )

    # Defined rather than omitted on purpose: leaving it out would make
    # __abstractmethods__ non-empty and OutlookConnector un-instantiable,
    # so CONNECTORS["outlook"]() would die with a generic TypeError at
    # construction instead of this message at the point of real use.
    def count_since(self, dt: datetime) -> int:
        raise NotImplementedError(
            "Outlook: same $filter as fetch_since, but request "
            "$count=true with the ConsistencyLevel: eventual header and "
            "read @odata.count instead of paging the messages."
        )


CONNECTORS = {"gmail": GmailConnector, "outlook": OutlookConnector}


# ---------------------------------------------------------------------------
# Smoke test + the watermark pattern your scheduler will use:
#   next_fetch_from = last_run_time - overlap (e.g. 5 min), dedupe by id.
#   Overlap guarantees no gaps; dedupe by Email.id kills the duplicates
#   the overlap creates. Gaps lose data silently; duplicates are cheap.
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    connector = GmailConnector()
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
