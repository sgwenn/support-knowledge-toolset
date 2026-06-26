---
date: 2026-06-24
topic: algolia-verdict-removal
status: ready-for-planning
---

# Remove DOC_GAP Verdict; Keep Public Doc Search as Silent Context

## Summary

Remove the DOC_GAP verdict and DOCS_UPDATE_DRAFT output from the agent pipeline while keeping the Algolia search tool registered and active. The agent continues to search docs.datadoghq.com as background context for drafting; TEEs stop seeing a verdict they can misread.

## Problem Frame

The agent currently produces a DOC_GAP output (COVERED / PARTIAL / GAP) alongside each KB article draft, indicating whether public Datadog documentation covers the ticket's topic. TEEs can mistake "GAP" to mean no documentation exists for the subject anywhere, rather than its actual meaning: that public docs at docs.datadoghq.com have incomplete coverage of this specific resolution. The verdict is ambiguous enough to cause misjudgment about whether an article is worth publishing.

The fix is to remove the verdict surface — not the underlying search. Public doc context still has value as background input to the drafting agent (accurate product terminology, awareness of what official docs already say about a feature). The confusion arises from surfacing a verdict, not from the agent reading docs.

## Requirements

**Output contract**

- R1. The agent no longer produces `DOC_GAP:` or `DOCS_UPDATE_DRAFT:` markers in its final output.
- R2. The SSE `done` event result no longer includes `doc_gap` or `docs_update_draft` fields.

**Frontend**

- R3. The DOC_GAP verdict UI is removed — no banner, no renderDocGap function, no gap-bar element.

**Prompt**

- R4. The SYSTEM_PROMPT step that instructs the agent to evaluate a DOC_GAP verdict is removed.
- R5. The SYSTEM_PROMPT step that instructs the agent to search docs.datadoghq.com is reframed: its stated purpose becomes "use public doc context to inform drafting accuracy" rather than "evaluate documentation gap."

**Tool registration**

- R6. The search_docs tool remains registered in CUSTOM_TOOL_DEFINITIONS and active in the agent loop — the agent retains access to public doc search.

## Key Decisions

**Keep the tool, remove only the verdict.** The original request was to stop surfacing public docs as output. The search itself adds value (the agent can cite accurate product documentation, align on official terminology, and reference relevant public URLs in the article body). Removing the tool would give up that value; removing only the verdict eliminates the confusion while preserving the benefit.

**Reframe step 4 rather than delete it.** Without DOC_GAP as a downstream consumer of the search results, the existing step 4 instruction would be an orphan — it would tell the agent to search without stating what to do with the results. Reframing the purpose keeps the instruction coherent.

## Scope Boundaries

- Removing search_docs from the agent tool definitions — out of scope; the tool stays active.
- Surfacing public doc search results in any other UI form — out of scope; search is background only.
- Changes to `tools/algolia.py` — not needed; the module is unchanged.
