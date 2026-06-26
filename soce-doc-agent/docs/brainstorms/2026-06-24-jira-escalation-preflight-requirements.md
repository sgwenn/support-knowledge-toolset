---
title: Jira Escalation Pre-flight Context Injection
date: 2026-06-24
status: draft
origin: docs/ideation/2026-06-24-agent-quality-ideation.html#idea-r1
---

# Jira Escalation Pre-flight Context Injection

## Problem

For escalated tickets, Jira often contains the true engineering root cause — the explanation that makes a KB article accurate. The agent is currently instructed to fetch this in its workflow step 2, but the lookup is stochastic: a model pass confident from Zendesk context alone may skip the Jira call with no code-level enforcement. When that happens, the generated article may misstate or omit the root cause.

Moving the Jira fetch out of the agent loop and into a deterministic Python pre-flight step eliminates the skipping risk: the agent receives Jira context unconditionally before any generation begins.

## Actors

**TEE (Technical Escalation Engineer)** — Uses the agent to draft KB articles from resolved escalated tickets. Sees a `tool_call` SSE event confirming Jira was fetched, then receives the article draft. Does not interact with the pre-flight step directly.

## Behavior

### Pre-flight trigger

Inside `run_kb_agent` (agent.py), before the `while True:` agent loop, add an async pre-flight block:

1. After receiving the Snowflake `fetch_ticket` result, check `ticket_data.get("is_escalated_to_jira_escalation")`.
2. If `True`: execute the Jira fetch (see below).
3. If `False` or not present: skip the pre-flight. The agent loop starts with the standard initial message.

The pre-flight runs only when the ticket result is already available (i.e., only in `mode == "ticket_id"`). For `mode == "raw_text"`, the pre-flight is skipped — there is no ticket ID to query Jira with.

### Jira fetch

Use `httpx.AsyncClient` with basic auth `(ATLASSIAN_USER_EMAIL, ATLASSIAN_API_TOKEN)` — the same credentials and pattern used in `run_publish_agent`. Query the Jira REST API:

```
GET https://datadoghq.atlassian.net/rest/api/3/issue?jql="Zendesk Ticket IDs" = {ticket_id}&fields=summary,description,status,assignee,comment
```

The JQL field `"Zendesk Ticket IDs"` is confirmed in the existing prompt at `prompts.py:12`.

### JIRA CONTEXT block

On success, append a labeled block to the content passed to `_build_initial_message` (or build it into the initial user message after `_build_initial_message` returns):

```
JIRA CONTEXT (pre-fetched — do not call Jira again):
Issue: {key} — {summary}
Status: {status}
Description: {description excerpt, max 800 chars}
Recent comment: {most recent comment body, max 400 chars, if present}
```

This block is appended to the first user message. The initial message now reads:

```
Generate a KB article for Zendesk ticket #{ticket_id}. Start by fetching it from Snowflake.

JIRA CONTEXT (pre-fetched — do not call Jira again):
Issue: DD-12345 — RUM session replay failing on Safari 17.4
...
```

### Failure behavior

If the Jira HTTP request fails for any reason (API error, no results, timeout, missing credentials):

- Omit the `JIRA CONTEXT` block silently. The initial message contains only the standard ticket instruction.
- Emit an SSE `tool_call` event with `name: "jira_preflight"` and `input_summary: "ticket #{ticket_id}"` before the attempt, and a `tool_result` event with `summary: "Jira lookup failed (skipped)"` after the failure — so the TEE sees the attempt was made and knows it fell back.
- Do **not** abort generation or surface an error to the TEE. The agent drafts from Zendesk context only.

Timeout for the pre-flight Jira request: 10 seconds (shorter than the Confluence publish timeout of 30s — Jira lookup is a single REST call).

### Prompt update (prompts.py)

Reframe SYSTEM_PROMPT workflow step 2 from an unconditional instruction to a conditional fallback:

> **Before:** "If `IS_ESCALATED_TO_JIRA_ESCALATION` is true, use the Atlassian MCP to find the linked Jira issue via JQL..."
>
> **After:** "If a `JIRA CONTEXT` block appears in the initial message, use it as the Jira source — do not re-call Jira. If `IS_ESCALATED_TO_JIRA_ESCALATION` is true and no `JIRA CONTEXT` block is present, use the Atlassian MCP to find the linked Jira issue via JQL: `"Zendesk Ticket IDs" = <ticket_id>`."

The Atlassian MCP remains registered in `_mcp_servers()` as the fallback path and for any future agent-loop Jira use.

### SSE events emitted by the pre-flight

| Event type | name / summary | When |
|---|---|---|
| `tool_call` | `"jira_preflight"` / `"ticket #<id>"` | Immediately before Jira HTTP request |
| `tool_result` | `"jira_preflight"` / `"Jira context loaded"` | On success |
| `tool_result` | `"jira_preflight"` / `"Jira lookup failed (skipped)"` | On any failure |

## Scope Boundaries

**In scope:**
- `run_kb_agent` in `agent.py` — pre-flight block before `while True:`
- `prompts.py` — reframe step 2 as a conditional fallback
- SSE events for the pre-flight attempt and result

**Not in scope:**
- Adding `JIRA_ISSUE_KEY` to the Snowflake `fetch_ticket` SQL query (separate change; not needed for this approach)
- Removing the Atlassian MCP from `_mcp_servers()` (kept as fallback)
- A general "context enrichment" layer for other pre-flight sources
- Any change to `run_publish_agent`, the publish flow, or Confluence interaction
- UI changes in `static/app.js` or `static/index.html` — the new `tool_call`/`tool_result` events use the existing tool event rendering path

## Success Criteria

1. On a ticket where `is_escalated_to_jira_escalation=True`, the SSE stream shows a `jira_preflight` `tool_call` event before any `thinking` event from the agent loop.
2. The final `DRAFT_HTML` for an escalated ticket references Jira-specific details (root cause language from the Jira issue) that would be absent if Jira had been skipped.
3. On a simulated Jira timeout (network block or invalid credentials), generation completes and the draft is produced without error — only the `tool_result` summary indicates the skip.
4. On a non-escalated ticket (`is_escalated_to_jira_escalation=False`), no `jira_preflight` events appear in the SSE stream and latency is unchanged.

## Assumptions

- `ATLASSIAN_API_TOKEN` and `ATLASSIAN_USER_EMAIL` are available in the environment for the Jira pre-flight call (same credentials used by `run_publish_agent`). If absent, the pre-flight silently skips.
- The JQL field `"Zendesk Ticket IDs"` is the correct field name in the Datadog Jira instance. Confirmed from existing SYSTEM_PROMPT at `prompts.py:12`.
- The Jira REST API is accessible from the application host (no firewall restriction that would block pre-flight calls from succeeding when MCP calls from the same environment succeed).
