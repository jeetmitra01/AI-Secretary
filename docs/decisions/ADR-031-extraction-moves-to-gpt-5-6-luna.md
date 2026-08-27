# ADR-031: Extraction moves to gpt-5.6-luna, provisionally

Date: 2026-08-26
Status: accepted (provisional — see "What would reverse this")

Amends ADR-029.

## Context

ADR-029, earlier the same day, kept extraction on `claude-sonnet-5` and
rejected `claude-haiku-4-5` because it lost 3 of 23 signal rows. Then
`gpt-5.6-luna` was scored on the same harness.

Both candidates, on the **same 397 emails** (the eval set grew to 497
mid-comparison when the 18:00 digest ran, so the figures below are the
intersection, not the two raw runs):

| | `haiku-4-5` | `gpt-5.6-luna` |
| --- | ---: | ---: |
| agreement with Sonnet | 97.0% | 93.5% |
| extraction failures | 0 | 0 |
| signal rows | 23 | 23 |
| **missed signal** | **3** | **3** |
| false alarms | 5 | 15 |
| cost / 100 emails (measured) | $0.205 | **$0.043** |

**The two models miss the identical three emails. Zero are missed by only
one of them.** Two models, different providers, different architectures,
different training data, agreeing exactly on which 3 of 23 signal rows are
`automated`. If those were independent errors the sets would collide by
chance about 1 time in 1,771.

That is the finding that moved this decision, and it cuts against ADR-029
rather than for it. ADR-029 asserts of two of those emails: *"(2) and (3)
are not judgment calls."* Two independent models made exactly that
judgment. The economical reading is now that the three are genuinely
ambiguous — bulk-sent marketing that happens to carry a real appointment —
and that **Sonnet's label may be the outlier**, not that two cheap models
share a defect.

Costs were re-measured with `count_tokens` rather than the harness's
4-chars-per-token estimate: 1,534 input and 102 output tokens per email.
The harness's own printed figures run ~10% low.

## Decision

**`extraction.MODEL = "gpt-5.6-luna"`.** Provisional, and taken by the
owner with the open risks below on the table.

**Only extraction moves.** Composition keeps its own `MODEL` and its
Anthropic client. `secretary_agent.py` (tool use) and `secretary_graph.py`
(streaming, ADR-025) stay Anthropic-native — the shim implements neither,
and routing them through it raises.

**`providers.py` is the only module that knows a second provider exists.**
`client_for(model)` returns an Anthropic client or an `OpenAIClient` that
answers to the same `messages.create(...)` interface. Every caller keeps
the ADR-005 shape: we still own the model call, and no framework hides it.

**`run_digest.ingest()` now builds two clients.** The `client` parameter
stays the Anthropic one and belongs to composition; extraction builds its
own from `extraction.MODEL`. Changing that one constant is the whole
switch — which is what makes reverting it cheap.

## Why

The owner's call, and the cost is real: at ~120 emails/day this is roughly
$0.05/day against Sonnet's $0.74/day once the introductory price ends on
2026-08-31 — a **14x** reduction.

What makes it defensible rather than merely cheap is the tie on the metric
ADR-029 called decisive. Luna and Haiku lose the same three, and those
three are now the best-evidenced *ambiguous* cases in the corpus rather
than clear misses. Luna is worse on agreement (93.5% vs 97.0%) but every
extra disagreement is a false alarm — a noisy digest line, the cheap
direction of ADR-016's asymmetry.

Zero extraction failures across 497 calls also matters: the tripwires in
`extraction.py` never fired, so schema discipline is not the concern it
would be with an unfamiliar provider.

## Risks accepted, explicitly

These are not hedges. Each one is a real cost of this decision.

- **`proposed_times` was never measured.** The harness compares `intent`
  only. A model can label `meeting_request` correctly and still extract
  the time wrongly, and `proposals.py` gates the ADR-023 calendar write on
  exactly that field. **This is the largest open risk**, and it is the
  first thing to close.
- **3x the false alarms** (15 vs 5 per 397). Cheap per item, but it makes
  the digest noisier, and ADR-016's tolerance for noise is not unlimited.
- **Real email bodies now go to a second processor.** ADR-030 was written
  hours earlier about exactly this class of disclosure. The owner added
  the key deliberately; it is recorded here because it is a data-handling
  change, not just a model change.
- **`openai` and `httpx2` are undeclared production dependencies.** There
  is no requirements file, so the scheduled task works only because these
  are installed in the one interpreter `run_digest.bat` names.
- **A workaround now runs unattended.** `providers.py` sends
  `Accept-Encoding: identity` because this environment's `httpx2`
  decompressor raises `TypeError` on real completions and the SDK
  re-raises it as a misleading `APIConnectionError`. That is a dependency
  bug being routed around twice a day at 08:00 and 18:00.
- **A provider outage degrades a run instead of stopping it.** Every
  email becomes an `ExtractionFailure`, the digest reports them (tripwire
  3), and the watermark still advances if the window was covered. Bodies
  are in the store (ADR-019), so it is recoverable by re-extraction — but
  nothing re-extracts automatically today.

## Rejected

- **Keep Sonnet 5 and do nothing.** ADR-029's position. Weakened by the
  both-models-agree finding, and 14x more expensive.
- **Switch to Haiku 4.5 instead.** Better agreement (97.0%) and a third of
  the false alarms, but 4.8x Luna's cost and *the same three misses* — it
  buys tidiness on the cheap axis, not safety on the expensive one.
- **Run Opus 5 first to settle who the outlier is.** The right experiment
  and still open: $5.09 for the full set, or ~$0.26 for the 25 signal rows
  that actually decide it. Deferred, not refused — it needs a
  `--signal-only` flag the harness does not have.
- **Put the shim in `extraction.py` with a provider branch.** Would spread
  provider knowledge across the pipeline and make ADR-005's "we own the
  model call" progressively less true. One module, one question.
- **Move composition too.** Never measured. The digest prose is what the
  owner actually reads, and there is no evidence at all about it.
- **Adopt the OpenAI SDK's own interface throughout.** That is a rewrite
  of `extraction.py`, `composition.py`, and the eval harness to serve one
  stage, and it would strand the Anthropic-native agent and graph.

## Consequences

- `python eval_extraction.py --model claude-sonnet-5` now scores the
  *former* incumbent as a candidate. The stored labels are still Sonnet's,
  so the harness still measures agreement-with-Sonnet — the baseline did
  not move just because the default did.
- Every new extraction row in `secretary.db` is Luna's output. Labels in
  the store are therefore no longer homogeneous, and a future bake-off
  scores against a mixture. **Anything comparing against the store should
  be run before the mixture grows.**
- `providers.py` has no `__main__`, so the ADR-021 console test does not
  cover it. Nothing in it prints.
- Reverting is one line: set `extraction.MODEL` back. The two-client split
  in `ingest()` is correct either way and stays.

## What would reverse this

Any one of these, and this ADR gets superseded rather than defended:

1. `proposed_times` measured and materially worse than Sonnet's.
2. An Opus run on the 25 signal rows agreeing with Sonnet on the disputed
   three — that would restore ADR-029's premise and make Luna's misses
   real misses.
3. A missed meeting in production that Sonnet would have caught.
4. False alarms rising enough to make the digest unread.
