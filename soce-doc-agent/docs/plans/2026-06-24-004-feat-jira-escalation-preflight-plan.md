---
title: "feat: Jira Escalation Pre-flight Context Injection"
date: 2026-06-24
origin: docs/brainstorms/2026-06-24-jira-escalation-preflight-requirements.md
---

# feat: Jira Escalation Pre-flight Context Injection

## Summary

Add a Python pre-flight step to `run_kb_agent` that fetches the linked Jira issue before the agent loop starts for escalated tickets, injecting the result as a `JIRA CONTEXT` block in the first user message. The agent receives Jira data unconditionally — it cannot skip it. On failure, generation continues silently from Zendesk context alone.

---

## Problem Frame

The agent is instructed to look up the Jira issue in its workflow step 2, but nothing in the code enforces that call. A confident model pass may skip it. Jira data is the highest-signal source for escalated tickets — it often contains the engineering root cause. The pre-flight moves this lookup outside the non-deterministic agent loop and into guaranteed Python execution.

---

## Requirements Trace

| Requirement | Source |
|---|---|
| Pre-flight triggers on `is_escalated_to_jira_escalation=True`, `mode == "ticket_id"` only | Behavior — Pre-flight trigger |
| JQL lookup by Zendesk ticket ID; no Snowflake schema change | Jira fetch |
| JIRA CONTEXT block appended to first user message, not injected via system prompt | JIRA CONTEXT block |
| Silent fail: omit block, emit SSE skip event, continue generation | Failure behavior |
| Prompt step 2 reframed as conditional fallback; Atlassian MCP kept registered | Prompt update |
| SSE tool_call + tool_result events emitted regardless of success/failure | SSE events table |

---

## Key Technical Decisions

**Pre-flight requires an extra Snowflake call for escalated tickets.**
`is_escalated_to_jira_escalation` is only available after fetching the ticket, but the ticket fetch currently happens inside the agent loop. The pre-flight must call `fetch_ticket` in Python before the loop starts, then the agent calls `fetch_ticket_from_snowflake` again as its normal first action. Two Snowflake reads per escalated ticket is the cost of keeping the agent loop's behavior unchanged.
*Rationale*: Alternatives that intercept the in-loop tool result or pass pre-fetched ticket data forward touch the agent loop's message construction more invasively. The double call is a cheap read-only query; the implementation simplicity is worth it.

**Jira description and comments are Atlassian Document Format (ADF), not plain text.**
The Jira REST API v3 returns `description` and `comment.body` as ADF JSON. The pre-flight must extract text content from ADF nodes before injecting into the message. A lightweight recursive text-node walker in Python is sufficient — no external library needed.

**`_jira_preflight` is a standalone async function, not an inline block.**
Extracting it as `async def _jira_preflight(queue, ticket_id)` keeps `run_kb_agent` readable and isolates the error handling boundary. The function receives the SSE queue to emit events and returns the JIRA CONTEXT block string (empty on failure).

**Jira search endpoint: `/rest/api/3/issue/search` with JQL as a query param.**
The JQL field `"Zendesk Ticket IDs"` is confirmed in the current SYSTEM_PROMPT. Implementation must verify the exact endpoint behavior against the Jira REST API v3 docs — specifically whether the JQL query requires URL-encoding and whether `maxResults=1` is sufficient.

---

## High-Level Technical Design

Current flow (escalated ticket):

```
run_kb_agent
  └─ while True:
       └─ Agent loop
            ├─ tool: fetch_ticket_from_snowflake  → ticket data (step 1)
            ├─ tool: Atlassian MCP (maybe)        → Jira issue (step 2, stochastic)
            └─ tool: ...                          → draft
```

New flow (escalated ticket):

```
run_kb_agent
  ├─ Pre-flight:
  │    ├─ fetch_ticket() [Python, sync via executor]  → check is_escalated flag
  │    ├─ _jira_preflight(queue, ticket_id) [async]
  │    │    ├─ SSE: tool_call "jira_preflight"
  │    │    ├─ httpx.AsyncClient → /rest/api/3/issue/search?jql=...
  │    │    ├─ ADF text extraction
  │    │    ├─ SSE: tool_result "Jira context loaded" | "Jira lookup failed (skipped)"
  │    │    └─ returns JIRA CONTEXT block string (or "")
  │    └─ Append JIRA CONTEXT to initial message (if non-empty)
  └─ while True:
       └─ Agent loop
            ├─ tool: fetch_ticket_from_snowflake  → ticket data (step 1, still needed)
            ├─ [skips Jira call if JIRA CONTEXT present in message]  ← updated prompt
            └─ tool: ...                          → draft
```

---

## Implementation Units

### U1. Jira pre-flight helper and ADF extractor

**Goal:** Add two new functions to `agent.py`: `_extract_adf_text(node)` for ADF → plain text, and `_jira_preflight(queue, ticket_id)` that fetches the Jira issue and returns a formatted JIRA CONTEXT block string.

**Requirements:** Jira fetch, JIRA CONTEXT block, Failure behavior, SSE events

**Dependencies:** none

**Files:**
- `agent.py` (modify — add two functions)

**Approach:**
- `_extract_adf_text(node)` — recursive function: if node type is `text`, return the `text` field; recurse into `content` array for block nodes; join with newlines. Handles `None` and non-dict inputs safely by returning `""`. No external library.
- `_jira_preflight(queue, ticket_id)` — async function:
  1. Emit SSE `tool_call` event with `name="jira_preflight"`, `input_summary=f"ticket #{ticket_id}"`.
  2. Read `ATLASSIAN_API_TOKEN` and `ATLASSIAN_USER_EMAIL` from env. If either is absent, skip to step 5.
  3. `GET /rest/api/3/issue/search` with `jql='"Zendesk Ticket IDs" = {ticket_id}'`, `fields="summary,description,status,comment"`, `maxResults=1`, timeout 10s. Use `httpx.AsyncClient` with `auth=(auth_email, token)`.
  4. Parse response: extract `issues[0]` key, summary, status name, description (via `_extract_adf_text`, truncated to 800 chars), most recent comment body (via `_extract_adf_text`, truncated to 400 chars). Build the JIRA CONTEXT block string.
  5. On any exception (missing creds, HTTP error, empty `issues`, timeout): emit SSE `tool_result` with `summary="Jira lookup failed (skipped)"`, return `""`.
  6. On success: emit SSE `tool_result` with `summary="Jira context loaded"`, return the block string.
- Import `httpx` locally inside `_jira_preflight` (consistent with `run_publish_agent`'s local import pattern).
- `_summarize_input` and `_summarize_result` do not need changes — `jira_preflight` events are emitted directly by the helper, not routed through `_dispatch_tool`.

**Patterns to follow:** `run_publish_agent` — httpx.AsyncClient basic-auth pattern, local httpx import, exception handling shape.

**Test scenarios:**
- Happy path: mock Jira API returning one issue with ADF description and one comment → verify the returned string contains "JIRA CONTEXT", the issue key, summary, status, truncated description, and recent comment.
- No issues found: mock Jira API returning `{"issues": []}` → verify empty string returned and "Jira lookup failed (skipped)" SSE event emitted.
- HTTP error (4xx/5xx): mock httpx raising `HTTPStatusError` → verify empty string returned, skip SSE event emitted, no exception propagates.
- Timeout: mock httpx raising `TimeoutException` → verify empty string returned, skip SSE event.
- Missing env vars (`ATLASSIAN_API_TOKEN` not set): verify empty string returned, skip SSE event, no HTTP call made.
- ADF text extraction: `{"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Root cause: X"}]}]}` → verify returns `"Root cause: X"`.
- ADF truncation: description > 800 chars → verify truncated to 800 chars; comment > 400 → truncated to 400.
- `_extract_adf_text(None)` → returns `""`. `_extract_adf_text("")` → returns `""`.

**Verification:** Unit-testable once test infrastructure is added. Until then, manual: call `_jira_preflight` with a known escalated ticket ID and inspect the returned string and SSE events in the stream.

---

### U2. Integrate pre-flight into `run_kb_agent`

**Goal:** Wire the pre-flight into `run_kb_agent` so that escalated `ticket_id`-mode requests automatically receive Jira context in their first message before the agent loop starts.

**Requirements:** Pre-flight trigger, JIRA CONTEXT block (first message construction), Failure behavior

**Dependencies:** U1

**Files:**
- `agent.py` (modify — `run_kb_agent` body)

**Approach:**
The change inserts a pre-flight block between the function's setup and the `while True:` loop:

1. After `component_holder` is initialized, add a pre-flight section:
   - If `mode == "ticket_id"` and `ticket_id` is set: call `fetch_ticket(ticket_id)` via `loop.run_in_executor` to get the ticket row without going through the agent. Check `ticket_data.get("is_escalated_to_jira_escalation")`.
   - If `True`: `jira_block = await _jira_preflight(queue, ticket_id)`. Otherwise `jira_block = ""`.
   - If `mode != "ticket_id"`: `jira_block = ""` (no pre-flight for raw_text mode).

2. Build the initial message:
   - `initial_msg = _build_initial_message(mode, ticket_id, raw_text)` (unchanged function).
   - If `jira_block`: `initial_msg += "\n\n" + jira_block`.
   - `messages = [{"role": "user", "content": initial_msg}]`.

The existing `messages = [{"role": "user", "content": _build_initial_message(...)}]` line is replaced with this two-step construction.

**Trade-off documented:** The pre-flight Snowflake call is an additional read for escalated tickets. The agent loop still calls `fetch_ticket_from_snowflake` as its first tool use. This is a read-only, non-mutating Snowflake call and the duplication is acceptable.

**Patterns to follow:** `loop.run_in_executor(None, lambda: fetch_ticket(ticket_id))` — same pattern used for the Anthropic API call in the existing loop.

**Test scenarios:**
- Integration: on a ticket with `is_escalated_to_jira_escalation=True`, first user message in `messages` contains "JIRA CONTEXT".
- Non-escalated ticket: `is_escalated_to_jira_escalation=False` → first user message is identical to current behavior; no SSE `jira_preflight` events emitted.
- `mode == "raw_text"`: no pre-flight Snowflake call; `messages` built normally from `raw_text`.
- Snowflake returns `None` (ticket not found): `is_escalated_to_jira_escalation` check evaluates to `False`; no Jira call; `messages` built normally.
- Pre-flight Jira call fails: `jira_block == ""`; `messages` built from `_build_initial_message` alone; generation proceeds normally.

**Verification:** Run the agent on a known escalated ticket; inspect SSE stream — `jira_preflight` tool_call and tool_result events appear before the first `thinking` event. Run on a non-escalated ticket — no `jira_preflight` events.

---

### U3. Update SYSTEM_PROMPT Jira workflow step

**Goal:** Reframe SYSTEM_PROMPT step 2 from an unconditional lookup instruction to a conditional fallback that defers to the pre-injected JIRA CONTEXT block when present.

**Requirements:** Prompt update

**Dependencies:** none (can be done in parallel with U1/U2 or after)

**Files:**
- `prompts.py` (modify — step 2 text only)

**Approach:**
Replace step 2 from:
> "2. If IS_ESCALATED_TO_JIRA_ESCALATION is true, use the Atlassian MCP to find the linked Jira issue via JQL: `"Zendesk Ticket IDs" = <ticket_id>` …"

To:
> "2. If a `JIRA CONTEXT` block appears in this message, use it as the Jira source for the article — do not re-call Jira. If `IS_ESCALATED_TO_JIRA_ESCALATION` is true and no `JIRA CONTEXT` block is present (pre-flight was unavailable), use the Atlassian MCP to find the linked Jira issue via JQL: `"Zendesk Ticket IDs" = <ticket_id>`."

No other prompt changes. The Atlassian MCP tool description in `CUSTOM_TOOL_DEFINITIONS` remains unchanged.

**Test scenarios:**
- Prompt text contains "JIRA CONTEXT block appears in this message" — confirm by reading the updated SYSTEM_PROMPT.
- Prompt text retains the JQL fallback with `"Zendesk Ticket IDs" = <ticket_id>` — confirms fallback path is documented.

**Verification:** Read `prompts.py` after edit; confirm step 2 matches the two-branch structure above. End-to-end: run agent on escalated ticket with Jira pre-flight succeeding — confirm agent loop emits no Atlassian MCP Jira search tool call in the SSE stream.

---

## Scope Boundaries

**In scope:**
- New `_extract_adf_text` and `_jira_preflight` functions in `agent.py`
- Pre-flight block in `run_kb_agent`
- SYSTEM_PROMPT step 2 update in `prompts.py`

**Not in scope:**
- Adding `JIRA_ISSUE_KEY` to Snowflake `fetch_ticket` SQL (separate change; not needed for this approach — see origin doc)
- Removing `_mcp_servers()` or the Atlassian MCP registration (kept as fallback)
- A general "context enrichment" abstraction layer for future pre-flight sources
- Changes to `run_publish_agent`, the publish flow, or any Confluence logic
- UI changes in `static/app.js` or `static/index.html` — the new SSE events render through the existing tool event display path unchanged

### Deferred to Follow-Up Work

- Add unit test infrastructure (`pytest`, `pytest-asyncio`) and a test file for `agent.py`. The test scenarios in U1 are written against the expected implementation; they are ready to port to pytest once a test harness is established.
- Add `JIRA_ISSUE_KEY` to the Snowflake query to enable a faster direct key-based lookup in a future iteration, bypassing JQL.

---

## Risks & Dependencies

| Risk | Likelihood | Mitigation |
|---|---|---|
| JQL field `"Zendesk Ticket IDs"` may have a different exact name in the Datadog Jira instance | Low (confirmed in current SYSTEM_PROMPT) | Verify at implementation time with a live JQL test; update if needed |
| ADF format may include block types not handled by the text walker (tables, code blocks, media) | Medium | Text walker returns content from `text` nodes only; other block types produce empty text — acceptable for KB generation context |
| Jira API rate limits affecting pre-flight at high volume | Low (hackathon scale) | Silent skip on any error; rate limit errors surface as "Jira lookup failed (skipped)" in SSE, generation continues |
| Double Snowflake call adds latency for escalated tickets | Low | Both calls are read-only cached queries; measured impact expected < 500ms |

---

## Sources & Research

- Origin requirements document: `docs/brainstorms/2026-06-24-jira-escalation-preflight-requirements.md`
- Ideation source: `docs/ideation/2026-06-24-agent-quality-ideation.html#idea-r1`
- Patterns followed: `run_publish_agent` in `agent.py` (httpx.AsyncClient + Atlassian credentials pattern)
- Existing JQL field name: `prompts.py:12` — `"Zendesk Ticket IDs" = <ticket_id>` confirmed in current SYSTEM_PROMPT
- Claim verification: all six structural claims verified against codebase (session context)
