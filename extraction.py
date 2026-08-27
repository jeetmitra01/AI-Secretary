"""
Phase 1, stage 1: per-email extraction (ADR-009).

Deliberately boring: one email in, one validated JSON out. No tools,
no loop, no thinking. Boring is what makes it testable.

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

MODEL = "gpt-5.6-luna"       # ADR-031, provisional. Measured against the
                              # store at 93.5% agreement with Sonnet 5 and
                              # 1/14th the cost. `providers.client_for()`
                              # builds the right client; nothing else here
                              # knows the provider changed.
                              #
                              # `python eval_extraction.py` replays the
                              # store through a candidate and scores it.

# Which models need — and accept — an explicit thinking off-switch.
#
# Not cosmetic, and not the same answer for every model:
#   claude-sonnet-5   omitting `thinking` runs ADAPTIVE thinking. So the
#                     off-switch is required, and `{"type": "disabled"}` is
#                     the accepted form.
#   claude-haiku-4-5  omitting `thinking` already means no thinking, and its
#                     thinking API is the older `{"type": "enabled",
#                     "budget_tokens": N}` shape. `disabled` is not part of
#                     it, so sending it risks a 400 to buy nothing.
#
# Hence: send the key only for models on this list, never by default. A new
# model goes here only after checking which of the two rules it follows.
THINKING_OFF = {"claude-sonnet-5", "claude-opus-5"}
#
# claude-opus-5 is listed even though it is not the default: on it,
# omitting `thinking` runs ADAPTIVE thinking, and max_tokens caps
# thinking + answer together, so an unlisted Opus run would truncate
# most of its JSON and cost ~3x. The list is a safety net, not a
# record of what is in use.
#
# OpenAI models are absent on purpose: the off-switch there is
# reasoning_effort="none", which providers.py sets. Adding one here
# would send `thinking` to a client that raises on it — loudly,
# which is the intended behaviour.


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


def extract_email(client, email: Email,
                  model: str = MODEL) -> Extraction | ExtractionFailure:
    """One email -> one validated Extraction. Never raises; failures
    return ExtractionFailure so the pipeline keeps moving.

    `model` is a parameter and not just the module constant so the eval
    harness can score a candidate against the SAME code path the pipeline
    runs — a benchmark of a reimplementation would measure the wrong thing.
    """
    prompt = PROMPT.format(
        email_block=email.brief(body_chars=1500),  # ADR-004 delimiting
        email_id=email.id,
        schema=json.dumps(Extraction.model_json_schema(), indent=1),
    )
    try:
        # temperature is gone, not moved: this model rejects a non-default
        # value outright. It never guaranteed identical output anyway — the
        # determinism this stage actually relies on is the schema (tripwire
        # 2), not a sampling knob.
        #
        # thinking is disabled EXPLICITLY. Omitting it now means adaptive
        # thinking, and max_tokens caps thinking + answer together — 800
        # tokens shared with a reasoning pass is how you get a truncated
        # JSON object, i.e. a parse failure the tripwires would report as
        # a model error. Extraction is a fill-in-the-schema task; there is
        # nothing here to reason about.
        resp = client.messages.create(
            model=model, max_tokens=800,
            **({"thinking": {"type": "disabled"}}
               if model in THINKING_OFF else {}),
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
