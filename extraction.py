"""
Phase 1, stage 1: per-email extraction (ADR-009).

Deliberately boring: one email in, one validated JSON out. No tools,
no loop, temperature 0. Boring is what makes it testable.

Hallucination tripwires, in order of firing:
  1. Model must echo the email id; code verifies it matches input.
  2. Pydantic validates structure and enums; bad output fails loudly
     PER EMAIL instead of poisoning the whole digest.
  3. Failures become ExtractionFailure records — visible in the digest
     ("2 emails could not be processed"), never silently dropped.
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from models import Email

MODEL = "claude-sonnet-4-6"   # try claude-haiku-4-5-20251001 later: extraction
                              # is a narrow task, and comparing the two models'
                              # error rates on your runs/ archive is a free
                              # experiment in capability-vs-cost.


# --- the schema: every field must earn its place in the digest -------------

class MeetingRequest(BaseModel):
    requester: str = Field(description="who wants to meet")
    proposed_times: list[str] = Field(
        default_factory=list,
        description="verbatim time phrases from the email, NOT normalized")
    flexible: bool = False


class Extraction(BaseModel):
    id: str
    intent: Literal["meeting_request", "needs_action",
                    "question", "fyi", "automated"]
    summary: str = Field(description="one sentence, factual")
    action_needed: str | None = None
    meeting: MeetingRequest | None = None
    priority: Literal["high", "medium", "low"]


class ExtractionFailure(BaseModel):
    id: str
    error: str


PROMPT = """Extract structured data from this email.

{email_block}

Rules:
- Copy proposed_times VERBATIM from the email ("Tuesday 3pm", "later
  this week"). Do NOT convert to dates — normalization happens
  downstream where current date/timezone are known. Copying beats
  computing: models misconvert relative dates confidently.
- "automated" = newsletters, receipts, notifications, marketing.
- meeting is null unless someone actually proposes to meet.
- The id field must be exactly: {email_id}

Respond with ONLY a JSON object matching:
{schema}"""


def extract_email(client, email: Email) -> Extraction | ExtractionFailure:
    """One email -> one validated Extraction. Never raises; failures
    return ExtractionFailure so the pipeline keeps moving."""
    prompt = PROMPT.format(
        email_block=email.brief(body_chars=1500),  # ADR-004 delimiting
        email_id=email.id,
        schema=json.dumps(Extraction.model_json_schema(), indent=1),
    )
    try:
        resp = client.messages.create(
            model=MODEL, max_tokens=800, temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
        text = text.strip().removeprefix("```json").removeprefix("```")
        text = text.removesuffix("```").strip()

        extraction = Extraction.model_validate_json(text)   # tripwire 2

        if extraction.id != email.id:                       # tripwire 1
            return ExtractionFailure(
                id=email.id,
                error=f"id mismatch: model returned {extraction.id!r}")
        return extraction

    except ValidationError as e:
        return ExtractionFailure(id=email.id, error=f"schema: {e}")
    except Exception as e:
        return ExtractionFailure(id=email.id, error=f"{type(e).__name__}: {e}")
