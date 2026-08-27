"""
Model clients, one per provider (ADR-031).

The system speaks Anthropic's Messages API everywhere: `extract_email` and
`compose_digest` both call `client.messages.create(...)` and read Anthropic
content blocks, and ADR-005 says we own that call rather than hiding it
behind a framework. That stays true. What changed is that ONE stage —
extraction — now points at an OpenAI model, so something has to make an
OpenAI client answer to the Anthropic shape.

That something is here, and nowhere else. `client_for(model)` is the only
place in the project that knows a second provider exists; every caller
keeps writing `client.messages.create(...)` exactly as before.

WHAT THIS SHIM IS NOT. It covers the one call shape extraction uses:
a single user message, `max_tokens`, no tools, no streaming, no thinking.
It deliberately does NOT cover tool use (`secretary_agent.py`) or
streaming (`secretary_graph.py`, ADR-025) — those stay Anthropic-native,
and an attempt to route them through here raises rather than silently
degrading. Nor does it cover composition, which keeps its own Anthropic
model and passes `thinking` (see the TypeError below).
"""

from __future__ import annotations

from anthropic import Anthropic

# Prefix routing. Deliberately dumb: this is the only question the project
# ever asks about providers, and a registry would be five lines of
# ceremony around one startswith().
OPENAI_PREFIX = "gpt-"

# The analogue of `thinking={"type": "disabled"}` on the Anthropic side
# (extraction.THINKING_OFF). gpt-5.6-luna accepts
# 'none' | 'low' | 'medium' | 'high' | 'xhigh'.
#
# Not a tuning knob. Extraction is a fill-in-the-schema task with nothing
# to reason about, and reasoning tokens bill as output — leaving this
# unset would spend money to make the JSON slower and no better.
OPENAI_REASONING_EFFORT = "none"


class _TextBlock:
    """Quacks like an Anthropic text content block."""
    type = "text"

    def __init__(self, text: str):
        self.text = text


class _ShimResponse:
    def __init__(self, text: str):
        self.content = [_TextBlock(text)]


class _ShimMessages:
    def __init__(self, client):
        self._client = client

    def create(self, *, model, max_tokens, messages, **unsupported):
        # Fail loud rather than silently dropping an Anthropic-only knob.
        # composition.py passes `thinking` unconditionally, so if a future
        # edit ever hands the composition call an OpenAI client, this is
        # the line that says so instead of quietly changing the digest.
        if unsupported:
            raise TypeError(
                f"OpenAI client received Anthropic-only arguments "
                f"{sorted(unsupported)} for {model!r}. Map them here "
                f"deliberately, or give that stage an Anthropic client.")

        resp = self._client.chat.completions.create(
            model=model,
            max_completion_tokens=max_tokens,   # `max_tokens` is rejected
            reasoning_effort=OPENAI_REASONING_EFFORT,
            messages=messages,                  # same {role, content} shape
        )
        choice = resp.choices[0]
        if getattr(choice.message, "refusal", None):
            raise RuntimeError(f"refusal: {choice.message.refusal}")
        if choice.finish_reason == "length":
            # Named explicitly. Otherwise a truncated object reaches
            # extraction.py's tripwire 2 looking like the model cannot
            # follow a schema, and the real cause (max_tokens too low)
            # never appears in the failure text.
            raise RuntimeError("truncated at max_completion_tokens")
        return _ShimResponse(choice.message.content or "")


class OpenAIClient:
    """An OpenAI client wearing an Anthropic client's interface."""

    def __init__(self, client):
        self.messages = _ShimMessages(client)


def client_for(model: str):
    """The right client for `model`. Anthropic unless it looks like OpenAI.

    Callers hold the result and reuse it; both underlying SDKs are safe to
    share across the ADR-028 worker pool.
    """
    if not model.startswith(OPENAI_PREFIX):
        return Anthropic()

    from openai import OpenAI          # imported lazily so an
                                       # Anthropic-only deployment does
                                       # not need the package installed
    # Accept-Encoding: identity is a WORKAROUND, not a preference.
    #
    # This environment has openai 3.3.1 -> httpx2 2.12.0, whose response
    # decompressor calls process(output_buffer_limit=...) against a
    # compression library that takes no keyword arguments. Small error
    # bodies decode fine; real completions come back compressed and raise
    # TypeError, which the SDK re-raises as a misleading
    # APIConnectionError. Asking for an uncompressed response skips the
    # broken decoder entirely.
    #
    # Cost: more bytes over the wire, which is nothing next to a scheduled
    # run that fails at 18:00 with a connection error that is not one.
    # Delete this line once the dependency is fixed, and re-test.
    return OpenAIClient(OpenAI(max_retries=2, timeout=60.0,
                               default_headers={"Accept-Encoding": "identity"}))
