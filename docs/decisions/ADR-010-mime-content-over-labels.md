# ADR-010: Trust MIME content over MIME labels

Date: 2026-07-27
Status: accepted

## Context

`Email.body_text` is contractually plain text — the connector ABC
promises callers "bodies are plain text (no HTML)", and that body goes
straight into LLM prompts via `Email.brief()`.

The Gmail smoke test broke that promise. A Robinhood newsletter arrived
as `multipart/alternative` with a `text/plain` part containing a full
HTML document (50,762 bytes — larger than the sibling `text/html` part
at 40,255). `_walk_parts` classified by `mimeType`, so `plain` came back
non-empty and `_extract_body` returned it before `_strip_html` ever ran.
Callers received raw markup.

Because `brief()` truncates to 120-200 chars, the summarizer saw
`<!doctype html><html xmlns=...` and zero actual content for every ESP
that does this. It also maximizes prompt-injection surface: raw markup
and tracking URLs instead of text.

Routing the mislabeled part to the HTML bucket alone made it worse. Both
parts then landed in `html` and were concatenated, so the newsletter got
emitted twice. That exposed a second bug: `_walk_parts` concatenated all
children unconditionally, but `multipart/alternative` means "competing
renderings of the same content — pick one." Prefer-plain had been
masking it by discarding the HTML copy.

## Decision

Two changes in `_walk_parts`:

1. A `text/plain` part whose content starts with `<!doctype` or `<html`
   (`_looks_like_html`) is routed to the HTML bucket and stripped.
2. `multipart/alternative` selects the longest candidate of each kind
   instead of concatenating. Other containers (`mixed`, `related`) still
   concatenate — their parts are genuinely distinct.

## Why

Sender labels are untrusted input like any other part of an email. The
prefer-plain rule is still right; it just needs a truthful signal about
which parts are actually plain.

Verified against the live message: `plain` 50,663 -> 0, `html` 50,663
(not 90,819 — no duplication), body now reads "Claim DOGE, SOL, or
another coin with Robinhood Gold ($5/month)."

## Rejected

Tag-density heuristics: catch HTML sprinkled mid-body, which the prefix
check misses, but trade that for false positives on plaintext that
happens to discuss markup. Not worth it until a real message needs it.

Trusting `text/html` exclusively and ignoring `text/plain`: throws away
the clean text that well-behaved senders provide.

## Known gaps

- The prefix check only catches wholesale mislabeling. HTML entities in
  genuine plain parts still survive undecoded — Wayfair (`Don&#x27;t`)
  and Urban Outfitters (`&zwnj;`) both show this. Fix is `html.unescape()`
  on the plain branch, needing an import alias to avoid colliding with
  the local `html` variable.
- Preheader padding (zero-width joiners, soft hyphens) still dominates
  the first ~120 chars of many marketing emails, so previews show
  padding rather than content. See SoFi, UNIQLO, Coach.
- `max(..., key=len)` picks the longest alternative as a proxy for
  "richest." It is a heuristic, not a guarantee.
