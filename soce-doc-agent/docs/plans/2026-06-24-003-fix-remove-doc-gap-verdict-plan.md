---
title: "fix: Remove DOC_GAP verdict; keep public doc search as silent context"
type: fix
date: 2026-06-24
origin: docs/brainstorms/2026-06-24-algolia-verdict-removal-requirements.md
---

# fix: Remove DOC_GAP verdict; keep public doc search as silent context

## Summary

Remove the `DOC_GAP` and `DOCS_UPDATE_DRAFT` output markers from the agent pipeline while leaving the `search_docs` tool registered and active. TEEs stop seeing a verdict they can misread; the agent retains access to public doc context for drafting accuracy.

## Problem Frame

The agent produces a `DOC_GAP` verdict (COVERED / PARTIAL / GAP) alongside each KB draft. TEEs can mistake "GAP" to mean no documentation exists for the topic anywhere, rather than its actual meaning: that docs.datadoghq.com has incomplete coverage of this specific resolution. Removing the verdict surface — not the underlying search — eliminates the confusion while preserving the benefit.

See origin: `docs/brainstorms/2026-06-24-algolia-verdict-removal-requirements.md`

---

## Requirements

**Output contract**

- R1. The agent no longer produces `DOC_GAP:` or `DOCS_UPDATE_DRAFT:` markers in its final output.
- R2. The SSE `done` event result no longer includes `doc_gap` or `docs_update_draft` fields.

**Frontend**

- R3. The DOC_GAP verdict UI is removed — no banner, no `renderDocGap` function, no `#doc-gap-bar` element.

**Prompt**

- R4. The SYSTEM_PROMPT step that instructs the agent to evaluate a DOC_GAP verdict is removed (current step 7).
- R5. The SYSTEM_PROMPT step that instructs the agent to search docs.datadoghq.com (current step 4) is reframed: its stated purpose becomes "use public doc context to inform drafting accuracy" rather than "evaluate documentation gap."

**Tool registration**

- R6. `search_docs` remains registered in `CUSTOM_TOOL_DEFINITIONS` and active in the agent loop.

---

## Key Technical Decisions

- **Keep the tool, remove only the verdict.** `search_docs` stays in `CUSTOM_TOOL_DEFINITIONS`, `_dispatch_tool`, `_summarize_input`, `_summarize_result`, and the `from tools.algolia import search_docs` import. The "Searching Datadog docs" label in `TOOL_LABELS` also stays — the progress indicator is still accurate. (see origin)

- **Reframe step 4, delete step 7.** Without DOC_GAP as a downstream consumer, step 4's current instruction ("assess documentation gap") would be an orphan. Reframing its purpose keeps the instruction coherent. Step 7 (the gap evaluation itself) is deleted outright. Steps 1–6 remain; the workflow instruction "Return your response in EXACTLY this format — five markers" becomes three markers.

- **Quality rule for URL citations stays.** The rule "Only cite documentation URLs you received from `search_docs` — never fabricate links" remains in the prompt. The agent can still cite public doc URLs in the article body; the rule guards against hallucinated links regardless of whether a verdict is surfaced.

- **`_parse_draft` simplifies to 3-tuple.** The function returns `(title, existing_url, draft_html)`. The call site in `run_kb_agent` and the `done` event result dict both drop `doc_gap` and `docs_update_draft`.

---

## Implementation Units

### U1. Simplify `agent.py` output contract

**Goal:** Remove `doc_gap` and `docs_update_draft` from `_parse_draft` and from the `done` event result. Keep all `search_docs` wiring untouched.

**Requirements:** R1, R2, R6

**Dependencies:** none

**Files:**
- `agent.py` (modify)

**Approach:** Change `_parse_draft` to return a 3-tuple `(title, existing_url, draft_html)` — remove the `doc_gap` and `docs_update_draft` local variables and their parsing blocks. Update the destructuring at line 131 to match. Remove `doc_gap` and `docs_update_draft` from the `result` dict in the `done` event (lines 135–146). Everything else in the file is unchanged.

**Patterns to follow:** The existing `_parse_draft` parsing pattern for `DRAFT_TITLE:` and `EXISTING_URL:` — same split-on-marker approach, just fewer markers.

**Test scenarios:**
- Generate against any ticket; SSE stream contains a `tool_call` event with `name: search_docs` — the tool still fires.
- The `done` event JSON has `draft_title`, `existing_url`, `draft_html`, and `component` keys, and does NOT have `doc_gap` or `docs_update_draft` keys.
- `draft_html` is still populated and non-empty for a solvable ticket.

**Verification:** Run the server, generate a draft in the browser, inspect the `done` SSE event in devtools Network tab. Confirm the result object has no `doc_gap` or `docs_update_draft`.

---

### U2. Reframe and simplify `prompts.py`

**Goal:** Remove the DOC_GAP evaluation step and output markers from the prompt; reframe step 4's stated purpose.

**Requirements:** R1, R4, R5

**Dependencies:** U1 (so the prompt and parse logic are consistent when testing)

**Files:**
- `prompts.py` (modify)

**Approach:**

*Step 4 reframe* — Change the current instruction from search-and-evaluate-gap to search-for-drafting-context. New wording should convey: "Search docs.datadoghq.com with 2–3 targeted queries to gather public documentation context — use it to inform accurate product terminology and to identify what official docs already say about the feature or error."

*Step 7 deletion* — Remove the entire step 7 block (DOC_GAP evaluation with COVERED / PARTIAL / GAP verdicts).

*Output format* — Remove the `DOC_GAP:` and `DOCS_UPDATE_DRAFT:` lines. Change the preamble from "five markers, nothing else" to "three markers, nothing else." Updated format:

```
DRAFT_TITLE: <article title, plain text, 10 words max>
EXISTING_URL: <URL of closely matching existing Confluence article, or NONE>
DRAFT_HTML:
<full article HTML in Confluence storage format>
```

*Quality rule* — "Only cite documentation URLs you received from `search_docs` — never fabricate links" stays verbatim.

**Patterns to follow:** The existing numbered step format in `## Workflow`.

**Test scenarios:**
- Generate against a ticket; the agent's final text output (visible in `thinking` SSE events) contains no `DOC_GAP:` or `DOCS_UPDATE_DRAFT:` markers.
- The agent still calls `search_docs` (visible in the SSE stream as a `tool_call` event).
- `DRAFT_HTML:` is still present in the agent's final output and is parsed correctly.

**Verification:** After generation, check the `thinking` events in the SSE stream — the final agent text should end with `DRAFT_HTML:` and its content, with no gap-related markers above it.

---

### U3. Remove DOC_GAP UI from `static/app.js`

**Goal:** Remove the `renderDocGap` function and all its call sites.

**Requirements:** R3

**Dependencies:** none (can be done in parallel with U1/U2)

**Files:**
- `static/app.js` (modify)

**Approach:** Three targeted removals:

1. `resetProgress` function (lines 93–94): remove the two lines that hide and clear `#doc-gap-bar`:
   ```js
   document.getElementById('doc-gap-bar').style.display = 'none';
   document.getElementById('doc-gap-bar').innerHTML = '';
   ```

2. `handleEvent` (line 162): remove the call `renderDocGap(result.doc_gap || '', result.docs_update_draft || '');`

3. Remove the entire `renderDocGap` function (lines 368–396).

`TOOL_LABELS.search_docs: 'Searching Datadog docs'` stays — the progress indicator is still accurate.

**Patterns to follow:** No new pattern needed — these are straight deletions.

**Test scenarios:**
- After generation, no DOC_GAP banner or element appears in the UI.
- The "Searching Datadog docs" step still appears in the progress list during generation.
- The draft preview still renders normally.
- `resetProgress` (called at the start of a new run) does not throw a JS error referencing `#doc-gap-bar`.

**Verification:** Open browser devtools console during generation — no JS errors. Confirm no gap-related UI element appears after the draft loads.

---

### U4. Remove `#doc-gap-bar` from `static/index.html`

**Goal:** Remove the now-unused `#doc-gap-bar` div.

**Requirements:** R3

**Dependencies:** U3 (ensures no JS references the element before it's removed)

**Files:**
- `static/index.html` (modify)

**Approach:** Delete line 94:
```html
<div id="doc-gap-bar" style="display:none"></div>
```

**Patterns to follow:** None — single-line deletion.

**Test scenarios:**
- DOM contains no element with `id="doc-gap-bar"`.
- Preview panel layout is unaffected (the div was hidden by default and added no visible layout contribution).

**Verification:** Open browser devtools Elements tab after page load — confirm no `#doc-gap-bar` element exists.

---

## Scope Boundaries

In scope: removing `DOC_GAP` / `DOCS_UPDATE_DRAFT` from prompt, parser, SSE output, and frontend.

Out of scope:
- Changes to `tools/algolia.py` — the module is unchanged.
- Removing `search_docs` from `CUSTOM_TOOL_DEFINITIONS` or `_dispatch_tool` — the tool stays active.
- Surfacing public doc search results in any other UI form — search remains background only.
- Deleting the superseded Direction B plan at `.claude/plans/dynamic-riding-honey.md` — leave for manual cleanup.
