---
title: "feat: Add service-management conversation fallback table"
date: 2026-06-24
type: feat
depth: lightweight
---

# feat: Add service-management conversation fallback table

## Summary

`fetch_ticket` and related Snowflake queries join exclusively to
`FACT_ZENDESK_TICKET_CONVERSATIONS_TECH_SUPPORT_AI`, which is populated only
for tech-support PPCs. Tickets in `service_mgmt_ppc` live in a separate
conversations table, so `FULL_CONVERSATION` comes back NULL and the KB agent
has no context to generate an article from.

The fix is to COALESCE over two LEFT JOINs — primary table first, fallback
table second — so one query works for all PPCs without conditional routing.

---

## Problem Frame

- `tools/snowflake.py:fetch_ticket` returns `full_conversation: None` for any
  ticket whose PPC is not covered by the primary conversations table.
- The KB agent (and `fetch_child_tickets`) silently degrades: it produces
  an article with no conversation evidence.
- Root cause: a second conversations table exists for service-management
  tickets but is never consulted.

---

## Requirements

| ID | Requirement |
|----|-------------|
| R1 | `fetch_ticket` returns a non-NULL `full_conversation` for service-mgmt tickets when the alternate table has data. |
| R2 | Tech-support PPC behavior is unchanged. |
| R3 | No new Python dependency. |
| R4 | Query does not double-count conversations (exactly one source per ticket). |
| R5 | `fetch_child_tickets` receives the same fix so child-ticket context is also populated. |

---

## Key Technical Decisions

**COALESCE two LEFT JOINs vs. dynamic table routing**
COALESCE is simpler and keeps a single SQL string. Dynamic routing (Python
`if ppc == 'service_mgmt_ppc': use table B`) would require callers to pass the
PPC before the query runs and would fork maintenance. COALESCE handles any
future PPC that moves tables without code changes.

**Alias convention**
Primary join alias `c` stays untouched. Alternate join alias `c2`. All
`c.*` column references become `COALESCE(c.col, c2.col)`.

**weekly_digest_candidates does not join conversations**
That query reads summary/investigation/solution columns from
`FACT_ZENDESK_TICKET_EMBEDDINGS`, not from the conversations table, so it is
out of scope.

---

## Scope Boundaries

**In scope**
- `tools/snowflake.py`: `fetch_ticket`, `fetch_child_tickets`

**Out of scope**
- `weekly_digest_candidates`, `find_similar_tickets`, `batch_novelty_check`
  (none join the conversations table)
- Agent prompt logic
- Frontend changes

---

## Implementation Units

### U1. Discover the alternate table name

**Goal:** Confirm the exact Snowflake table and schema that holds
service-management ticket conversations.

**Requirements:** Pre-condition for U2; unblocks everything.

**Dependencies:** none

**Files:** none (discovery only — no code change)

**Approach:**
Run the following via the Snowflake MCP (or `! snowsql`) to find the table:

```
SHOW TABLES LIKE '%CONVERSATION%' IN SCHEMA REPORTING.GENERAL;
SHOW TABLES LIKE '%CONVERSATION%' IN SCHEMA REPORTING.DLAC_RESTRICTED;
-- also try other schemas if needed
```

Then verify it has rows for service-mgmt tickets:

```sql
SELECT COUNT(*) FROM <candidate_table>
WHERE TICKET_ID IN (
    SELECT ID FROM REPORTING.GENERAL.DIM_ZENDESK_TICKET
    WHERE PRIMARY_PRODUCT_COMPONENT = 'service_mgmt_ppc'
    LIMIT 5
);
```

Record the fully-qualified table name (`SCHEMA.TABLE`) before proceeding.

**Test scenarios:**
- Confirm row count > 0 for at least one known service-mgmt ticket ID.

**Verification:** You have a table name you can paste into U2.

---

### U2. Add fallback join to `fetch_ticket`

**Goal:** `fetch_ticket` returns `full_conversation` from the alternate table
when the primary table has no row for the ticket.

**Requirements:** R1, R2, R4

**Dependencies:** U1

**Files:** `tools/snowflake.py`

**Approach:**
In the `fetch_ticket` SQL, add a second LEFT JOIN to the alternate table
(alias `c2`) and wrap every `c.` conversation column in `COALESCE(c.col, c2.col)`.
The ON clause for `c2` is identical to `c` except for the table name.

Directional sketch (replace `<ALT_TABLE>` with the name from U1):

```
-- existing
LEFT JOIN REPORTING.DLAC_RESTRICTED.FACT_ZENDESK_TICKET_CONVERSATIONS_TECH_SUPPORT_AI c
    ON t.ID = c.TICKET_ID
-- new
LEFT JOIN REPORTING.GENERAL.<ALT_TABLE> c2
    ON t.ID = c2.TICKET_ID
```

Columns to COALESCE: `FULL_CONVERSATION` (currently the only column selected
from `c` in `fetch_ticket`).

**Patterns to follow:** existing alias style in `tools/snowflake.py:56`.

**Test scenarios:**
- Fetch a known service-mgmt ticket: `full_conversation` is non-NULL.
- Fetch a known tech-support ticket: `full_conversation` still matches the
  primary table value (R2 — no regression).
- Fetch a ticket that exists in neither table: `full_conversation` is None
  (graceful null propagation).

**Verification:** Run `fetch_ticket(<service_mgmt_ticket_id>)` via a quick
Python snippet or the app's `/run` endpoint; confirm `full_conversation` has
text.

---

### U3. Apply the same fix to `fetch_child_tickets`

**Goal:** Child tickets for service-mgmt parent tickets also return
`full_conversation`.

**Requirements:** R5

**Dependencies:** U1, U2 (same pattern)

**Files:** `tools/snowflake.py`

**Approach:**
`fetch_child_tickets` already uses an INNER JOIN (not LEFT JOIN) on the
primary conversations table. Change the existing join to LEFT JOIN, add the
`c2` fallback LEFT JOIN, and COALESCE `FULL_CONVERSATION`.

Note: if the child-ticket query currently drops tickets without a conversation
row (because of INNER JOIN), this change intentionally widens the result set.
Verify the KB agent handles a NULL `full_conversation` gracefully for child
tickets — it likely does, since `fetch_ticket` already returns None when the
field is absent.

**Patterns to follow:** U2 approach.

**Test scenarios:**
- Fetch children of a service-mgmt parent ticket: at least one child has
  non-NULL `full_conversation` (if children exist).
- Fetch children of a tech-support ticket: behavior unchanged (R2).
- Parent with no children: empty list returned, no error.

**Verification:** Call `fetch_child_tickets(<service_mgmt_ticket_id>)` and
inspect the result.

---

## Open Questions

| # | Question | Status |
|---|----------|--------|
| Q1 | What is the fully-qualified name of the alternate conversations table? | **Blocking** — resolve in U1 before U2/U3 |
| Q2 | Does the alternate table have the same `FULL_CONVERSATION` column name? | Verify during U1 discovery |
| Q3 | Are there other columns in the alternate table the KB agent should use (e.g., a different summary field)? | Deferred — current scope is FULL_CONVERSATION parity only |

---

## Sources & Research

- `tools/snowflake.py:50-68` — `fetch_ticket` current JOIN
- `tools/snowflake.py:80-89` — `fetch_child_tickets` current JOIN
- Prior session: service_mgmt_ppc hang was caused by unbounded vector join in
  `batch_novelty_check`, not by this conversations gap — that fix is already
  applied
