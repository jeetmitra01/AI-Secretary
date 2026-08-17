"""
Phase 1, stage 2: digest composition.

Input is validated Extraction objects — never raw email bodies. An
injection in a body must survive stage-1 summarization AND this stage
to reach the user: a two-stage filter we get for free from ADR-009.

The counts/grouping are computed in CODE from the structured data;
the model only writes prose around facts we already verified. If the
prose ever disagrees with the counts, trust the counts — and save the
run as a regression case.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from extraction import Extraction, ExtractionFailure

MODEL = "claude-sonnet-5"
USER_TZ = ZoneInfo("Asia/Kolkata")


@dataclass(frozen=True)
class Coverage:
    """How much of the window this run actually read (ADR-012).

    Both numbers come from the connector, never the model, and both are
    measured over the SAME population: emails in the window that have not
    already been summarized by an earlier run. Mail we covered yesterday
    is neither pending nor missing, so it belongs in neither number.
    """
    total_matching: int    # count_since: unread-by-us, whole window
    fetched: int           # what fetch_since handed back, capped

    @property
    def truncated(self) -> bool:
        return self.missed > 0

    @property
    def missed(self) -> int:
        # Clamped: count_since runs first, so mail arriving between the
        # two calls can make fetched exceed total. That is a complete
        # window, not a negative shortfall.
        return max(0, self.total_matching - self.fetched)


def coverage_warning(coverage: Coverage | None) -> str:
    """The caveat ADR-012 exists to force. Scoping every negative to the
    covered slice is the whole point: "0 meeting requests" asserted over
    a third of the window is the failure that ADR was written about."""
    if not coverage or not coverage.truncated:
        return ""
    return (f"> ⚠ **Partial window: {coverage.fetched} of "
            f"{coverage.total_matching} emails read.** The other "
            f"{coverage.missed} were not fetched and are not summarized "
            f"below — read every \"none\" here as scoped to the "
            f"{coverage.fetched} covered.")

COMPOSE_PROMPT = """Write a concise email digest for your principal.
Current local time: {now} ({tz}).

Structured data extracted from {n} emails (already verified, cite ids):
{data}

Format (markdown):
## Meetings requested   <- who, their verbatim proposed times, id
## Needs your action    <- what and by when if stated, id
## FYI                  <- one line each, id; compress ruthlessly
Skip empty sections. Do not invent detail absent from the data.
Interpret verbatim time phrases relative to current local time where
unambiguous (e.g. "Tuesday 3pm" -> "Tue 4 Aug, 3:00 pm"), and keep
the original phrase in parentheses when you do."""


def compose_digest(client, extractions: list[Extraction],
                   failures: list[ExtractionFailure],
                   coverage: Coverage | None = None) -> str:
    now = datetime.now(USER_TZ)
    blocks = [f"# Digest — {now:%a %d %b, %H:%M}"]
    warning = coverage_warning(coverage)

    if not extractions and not failures:
        # "No new email" is a claim about the whole window, so it is only
        # true when the whole window was read. On a truncated fetch it is
        # exactly the unscoped negative ADR-012 was written about.
        if warning:
            blocks += [warning, "No new email among the ones read."]
        else:
            blocks.append("No new email.")
        return "\n\n".join(blocks)

    blocks.append(stats_line(extractions))
    if warning:
        blocks.append(warning)
    if failures:  # visible, never silent (extraction.py tripwire 3)
        blocks.append(f"> ⚠ {len(failures)} email(s) failed extraction: "
                      + ", ".join(f.id for f in failures))

    data = "\n".join(e.model_dump_json() for e in extractions)
    # Same two changes as extraction.py: temperature is rejected by this
    # model, and thinking must be turned off by name or it runs by default
    # and eats into these 2000 tokens. The counts above are computed in
    # code (stats_line), so what is left for the model here is prose — a
    # rewriting job, not a reasoning one.
    resp = client.messages.create(
        model=MODEL, max_tokens=2000,
        thinking={"type": "disabled"},
        messages=[{"role": "user", "content": COMPOSE_PROMPT.format(
            now=now.strftime("%A %d %B %Y, %H:%M"), tz=USER_TZ.key,
            n=len(extractions), data=data)}],
    )
    body = "".join(b.text for b in resp.content if b.type == "text")

    blocks.append(body.strip())
    return "\n\n".join(blocks)


def stats_line(extractions: list[Extraction]) -> str:
    """Computed in code — the numbers the toast and header show are
    never the model's numbers."""
    meetings = sum(1 for e in extractions if e.meeting)
    action = sum(1 for e in extractions if e.intent == "needs_action")
    return (f"**{len(extractions)} emails** — "
            f"{meetings} meeting request(s), {action} need action")


def toast_summary(extractions: list[Extraction],
                  coverage: Coverage | None = None) -> tuple[str, str]:
    """(title, one-liner) sized for a Windows toast."""
    meetings = sum(1 for e in extractions if e.meeting)
    action = sum(1 for e in extractions if e.intent == "needs_action")
    high = [e for e in extractions if e.priority == "high"]
    title = f"Digest: {meetings} meeting(s), {action} need action"
    line = high[0].summary[:100] if high else \
        (extractions[0].summary[:100] if extractions else "No new email")
    if coverage and coverage.truncated:
        # The toast is the only channel a scheduled run reaches you on,
        # so the shortfall has to survive the 100-char squeeze.
        line = f"⚠ {coverage.missed} not read. {line}"
    return title, line
