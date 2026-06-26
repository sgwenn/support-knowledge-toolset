---
title: "Hide Verdict Surface While Keeping Underlying Tool Active"
date: 2026-06-24
category: design-patterns
module: "agent.py / prompts.py / static/app.js / static/index.html"
problem_type: design_pattern
severity: medium
applies_when:
  - An agent tool produces correct intermediate output that is consistently misread or misinterpreted by end users, and the fix is to suppress the surfaced verdict while keeping the tool registered so the agent still benefits from it during drafting.
tags:
  - algolia
  - search_docs
  - doc-gap
  - sse-streaming
  - output-markers
  - ux
  - tee-workflow
  - kb-generation
---

# Hide Verdict Surface While Keeping Underlying Tool Active

## Context

The KB article generation agent in `soce-doc-agent` used Algolia search (`search_docs`) to query `docs.datadoghq.com` as part of its drafting pipeline. After searching, the agent was instructed to evaluate how well the official docs covered the specific resolution being documented — and to surface that verdict in its output alongside the KB article draft.

The output pipeline produced five marked fields:

```
DRAFT_TITLE: <title>
EXISTING_URL: <url or NONE>
DOC_GAP: COVERED | PARTIAL | GAP
DOCS_UPDATE_DRAFT: <suggested docs update or NONE>
DRAFT_HTML: <article body>
```

The `DOC_GAP` field was intended to signal whether `docs.datadoghq.com` had complete, partial, or no coverage of the *specific resolution path* documented in the KB article. A verdict of `GAP` was meant to mean: "the official docs exist but don't cover this particular edge case or fix." It was not intended to assert that no public documentation exists for the topic.

Technical escalation engineers (TEEs) reading the output consistently misread `GAP` as the broader claim — that the product had no documentation for the area at all. This misinterpretation was not a training issue; it was a labeling problem. The word "GAP" in the context of a verdict field naturally reads as an absolute state rather than a relative one. Confidence in the tool's output dropped because TEEs saw the verdict contradict what they knew about existing docs coverage.

The problem was compounded by `DOCS_UPDATE_DRAFT` — a second output field that generated a suggested patch to the official docs. When TEEs saw a drafted docs update alongside a `GAP` verdict, it reinforced the false reading: "the tool thinks there's no documentation and is trying to write it." The intent (improve incomplete coverage) read as (fill a total absence).

## Guidance

The fix removes the verdict surface entirely while keeping the underlying capability active.

**What was removed:**

- `DOC_GAP:` output marker and `COVERED / PARTIAL / GAP` verdict logic (from `prompts.py` Step 7)
- `DOCS_UPDATE_DRAFT:` output marker and its content (from `prompts.py`)
- `renderDocGap()` function in `static/app.js` (29 lines)
- The `#doc-gap-bar` DOM element in `static/index.html`
- The `doc_gap` and `docs_update_draft` fields from the `done` SSE event payload
- `_parse_draft` parsing of those two fields

**What was kept:**

- `search_docs` registered in `CUSTOM_TOOL_DEFINITIONS` in `agent.py`
- `search_docs` dispatch handler in `_dispatch_tool`
- `search_docs` entries in `_summarize_input` and `_summarize_result`
- `TOOL_LABELS['search_docs']` in `static/app.js` — the "Searching Datadog docs" progress indicator still fires
- The quality rule: *"Only cite documentation URLs you received from search_docs — never fabricate links"* (retained in `prompts.py`)
- Step 4 of the prompt, reframed: purpose is now to "inform accurate product terminology and understand what official docs cover — this context improves drafting accuracy"

**Before/after: `prompts.py` output format**

Before (5 markers):
```
DRAFT_TITLE: <title of the KB article>
EXISTING_URL: <url> or NONE
DOC_GAP: COVERED | PARTIAL | GAP
DOCS_UPDATE_DRAFT: <markdown patch> or NONE
DRAFT_HTML: <full article HTML>
```

After (3 markers):
```
DRAFT_TITLE: <title of the KB article>
EXISTING_URL: <url> or NONE
DRAFT_HTML: <full article HTML>
```

**Before/after: `_parse_draft` in `agent.py`**

Before (5-tuple):
```python
def _parse_draft(text: str) -> tuple[str, str, str, str, str]:
    # returns (title, existing_url, doc_gap, docs_update_draft, draft_html)
```

After (3-tuple):
```python
def _parse_draft(text: str) -> tuple[str, str, str]:
    # returns (title, existing_url, draft_html)
```

The `done` SSE event result object changed accordingly:

Before:
```json
{
  "draft_title": "...",
  "existing_url": "...",
  "doc_gap": "GAP",
  "docs_update_draft": "...",
  "draft_html": "..."
}
```

After:
```json
{
  "draft_title": "...",
  "existing_url": "...",
  "draft_html": "...",
  "component": "..."
}
```

## Why This Matters

**Drafting quality is preserved.** The `search_docs` tool call against Algolia indexes `docs.datadoghq.com` content. When the agent can look up official docs for a topic, it uses the product terminology, canonical feature names, and link targets from those docs when writing the KB article. Removing the tool entirely would have degraded the accuracy and credibility of the generated articles. The search result informs the draft — it just no longer produces a side-channel verdict about documentation completeness.

**Verdict surfaces have a high misinterpretation tax.** The COVERED/PARTIAL/GAP classification required the reader to hold a precise mental model of what the verdict axis meant. In practice, TEEs were context-switching between investigating a ticket, reading a generated article, and assessing the tool's output — they weren't reading the legend. A verdict that requires explanation to be interpreted correctly is a verdict that will be misread under operational load.

**Removing the surface is not the same as removing the capability.** The agent still searches docs on every run. The progress indicator still shows "Searching Datadog docs" during execution. The only change is that the result of that search is not rendered as a verdict for the user — it is consumed internally to improve the draft. This is a common pattern: use the tool's output to improve an artifact, don't also surface the tool's intermediate signal as a separate output.

## When to Apply

Apply the "keep the capability, remove the verdict" pattern when:

1. **The verdict requires domain context to parse correctly.** If a user needs to understand the tool's internal taxonomy (e.g., what "GAP" means relative to "PARTIAL") before the verdict is useful, the verdict is adding cognitive load rather than value.

2. **The capability drives output quality, not output selection.** When the point of a tool call is to inform how something is written rather than to produce a classification the user acts on, the verdict is noise. Route the tool's signal into the artifact, not into a parallel output field.

3. **The verdict is actionable by a different team, not the immediate user.** DOC_GAP was notionally useful for a docs team deciding what to update — but TEEs aren't that team. Surfacing an actionable verdict to someone who can't act on it creates confusion without benefit.

Do not apply this pattern when:

- The verdict IS the product. If the user opened the tool specifically to get a classification (e.g., a severity scorer, a routing tool), removing the verdict removes the value.
- The misinterpretation can be fixed with labeling. If a clearer label or tooltip would eliminate the confusion, prefer that. In this case, COVERED/PARTIAL/GAP was intrinsically ambiguous — "incomplete coverage of this resolution" is a nuanced concept that resists short labels.
- The capability and verdict are separable only in theory. If the two are tightly coupled in the model's instruction chain, removing the verdict while keeping the tool may destabilize the prompt. In this repo, `search_docs` is invoked to inform drafting (Step 4 of the prompt), and the verdict was a separate downstream step (Step 7) — they were cleanly separable.

## Examples

### Direction A vs. Direction B

During planning, two directions were considered:

**Direction B (rejected):** Remove `search_docs` entirely. Delete the tool from `CUSTOM_TOOL_DEFINITIONS`, remove its dispatch handler, remove it from the prompt, remove the import. This would eliminate any docs-related output and end the confusion.

**Direction A (chosen):** Keep `search_docs` active, remove only the verdict output. This preserves the drafting quality benefit while eliminating the confusing signal.

Direction B was rejected because the confusion was located in the output surface, not in the search capability itself. Algolia search improves the agent's drafting accuracy by grounding it in real product terminology. Removing the tool to solve a labeling problem would have been a disproportionate fix — trading a real quality benefit to avoid having to surface a verdict.

The plan file documenting Direction B is retained at `docs/plans/dynamic-riding-honey.md` as a record of the rejected alternative.

### Prompt change: Step 7 deletion

The entire Step 7 block was deleted from `prompts.py`. It previously read (paraphrased):

```
Step 7 — Evaluate the documentation gap:
Based on the search_docs results, determine whether docs.datadoghq.com covers
this resolution path. Output one of: COVERED, PARTIAL, or GAP.
If PARTIAL or GAP, draft a suggested update in DOCS_UPDATE_DRAFT.
```

This step is gone. The `search_docs` call still happens in Step 4, but its framing changed from "evaluate the gap" to "inform your drafting accuracy."

### Frontend removal

`renderDocGap()` in `static/app.js` was a 29-line function that read `doc_gap` from the `done` event and rendered a colored badge (`COVERED` = green, `PARTIAL` = yellow, `GAP` = red). The badge rendered in `#doc-gap-bar` in `static/index.html`. Both are gone. The "Searching Datadog docs" progress line in the tool label map remains, so users still see evidence that docs search ran — they just don't see a verdict about what it found.

## Related

- `docs/brainstorms/2026-06-24-algolia-verdict-removal-requirements.md` — origin requirements doc; defines R1–R6 that drove this change
- `docs/plans/2026-06-24-003-fix-remove-doc-gap-verdict-plan.md` — implementation plan derived from the brainstorm
