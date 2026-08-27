"""
Tests for the two-provider split (ADR-031).

Same split as the other test files here:

  1. INVARIANT — composition's call shape must RAISE on an OpenAI client.
     This is the test that actually enforces ADR-031's boundary. The
     shim's job is to be indistinguishable from an Anthropic client for
     the one call extraction makes; the danger is that it becomes
     indistinguishable for calls it does NOT faithfully implement, and
     composition is the live example — it passes `thinking`, which has no
     OpenAI equivalent. Silently dropping it would change the digest with
     nothing to show why.

  2. BEHAVIOUR — routing, parameter translation, and the failure paths
     (refusal, truncation) that extraction.py's tripwires depend on.

No network: the OpenAI client is a fake that records what it was handed.

Run:  python -m pytest tests -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import extraction                                       # noqa: E402
import providers                                        # noqa: E402


# --- fakes -----------------------------------------------------------------

class FakeMessage:
    def __init__(self, content, refusal=None):
        self.content = content
        self.refusal = refusal


class FakeChoice:
    def __init__(self, content, finish_reason="stop", refusal=None):
        self.message = FakeMessage(content, refusal)
        self.finish_reason = finish_reason


class FakeCompletions:
    def __init__(self, choice):
        self._choice = choice
        self.seen: dict = {}

    def create(self, **kw):
        self.seen = kw
        return type("R", (), {"choices": [self._choice]})()


class FakeOpenAI:
    def __init__(self, content="{}", finish_reason="stop", refusal=None):
        self.chat = type("C", (), {})()
        self.chat.completions = FakeCompletions(
            FakeChoice(content, finish_reason, refusal))


def shim(**kw) -> providers.OpenAIClient:
    return providers.OpenAIClient(FakeOpenAI(**kw))


# --- 1. INVARIANT ----------------------------------------------------------

def test_composition_call_shape_raises_on_an_openai_client():
    """composition.py sends `thinking={"type": "disabled"}` unconditionally.
    If a refactor ever hands it the OpenAI client, it must fail loudly."""
    client = shim()

    with pytest.raises(TypeError) as exc:
        client.messages.create(model="gpt-5.6-luna", max_tokens=2000,
                               thinking={"type": "disabled"},
                               messages=[{"role": "user", "content": "x"}])

    assert "thinking" in str(exc.value)


def test_openai_models_are_not_in_thinking_off():
    """The two off-switches must not both fire. An OpenAI model listed in
    THINKING_OFF would be sent `thinking` and raise on every email."""
    for model in extraction.THINKING_OFF:
        assert not model.startswith(providers.OPENAI_PREFIX), (
            f"{model} is in THINKING_OFF and routes to the OpenAI client; "
            f"its off-switch is reasoning_effort, set in providers.py")


def test_opus_is_in_thinking_off_even_though_it_is_not_the_default():
    """On claude-opus-5, omitting `thinking` runs adaptive thinking, and
    max_tokens caps thinking + answer together — an unlisted Opus run
    truncates most of its JSON and costs ~3x. Safety net, not a record of
    what is in use."""
    assert "claude-opus-5" in extraction.THINKING_OFF


# --- 2. BEHAVIOUR ----------------------------------------------------------

def test_client_for_routes_on_prefix():
    assert isinstance(providers.client_for("gpt-5.6-luna"),
                      providers.OpenAIClient)
    # Anything not OpenAI-shaped falls through to the real Anthropic client.
    assert not isinstance(providers.client_for("claude-sonnet-5"),
                          providers.OpenAIClient)


def test_max_tokens_is_translated_and_reasoning_is_disabled():
    client = shim(content='{"ok": true}')
    client.messages.create(model="gpt-5.6-luna", max_tokens=800,
                           messages=[{"role": "user", "content": "hi"}])

    seen = client.messages._client.chat.completions.seen
    assert seen["max_completion_tokens"] == 800   # `max_tokens` is rejected
    assert "max_tokens" not in seen
    assert seen["reasoning_effort"] == providers.OPENAI_REASONING_EFFORT
    assert seen["messages"] == [{"role": "user", "content": "hi"}]


def test_response_quacks_like_an_anthropic_response():
    """extract_email does: "".join(b.text for b in resp.content
    if b.type == "text"). Nothing else about the response is touched."""
    resp = shim(content='{"id": "abc"}').messages.create(
        model="gpt-5.6-luna", max_tokens=800,
        messages=[{"role": "user", "content": "hi"}])

    assert "".join(b.text for b in resp.content if b.type == "text") \
        == '{"id": "abc"}'


def test_truncation_is_named_rather_than_left_to_the_schema_tripwire():
    client = shim(content='{"id": "abc"', finish_reason="length")

    with pytest.raises(RuntimeError, match="truncated"):
        client.messages.create(model="gpt-5.6-luna", max_tokens=8,
                               messages=[{"role": "user", "content": "hi"}])


def test_refusal_is_surfaced():
    client = shim(content=None, refusal="I can't help with that")

    with pytest.raises(RuntimeError, match="refusal"):
        client.messages.create(model="gpt-5.6-luna", max_tokens=800,
                               messages=[{"role": "user", "content": "hi"}])


def test_none_content_does_not_crash_the_join():
    """A null content with no refusal must become an empty string, not
    None — extract_email would raise TypeError on the join otherwise, and
    the failure would read as a bug in extraction rather than an empty
    completion."""
    resp = shim(content=None).messages.create(
        model="gpt-5.6-luna", max_tokens=800,
        messages=[{"role": "user", "content": "hi"}])

    assert resp.content[0].text == ""
