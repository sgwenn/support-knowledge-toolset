# Generate KB Article

Fetch a Zendesk ticket from Snowflake and generate a Confluence-ready KB article.

## Usage
```
/generate-kb <ticket_id>
```

## Instructions

The user has provided a Zendesk ticket ID as `$ARGUMENTS`.

### Step 1 — Fetch from Snowflake

Run this query:

```sql
SELECT
    t.ID,
    t.SUBJECT,
    t.STATUS,
    t.PRIORITY,
    t.PRIMARY_PRODUCT_COMPONENT,
    t.CREATED_TIMESTAMP,
    t.SOLVED_TIMESTAMP,
    t.TICKET_COMPLEXITY,
    t.IS_ESCALATED_TO_ENGINEERING,
    c.FULL_CONVERSATION
FROM REPORTING.GENERAL.DIM_ZENDESK_TICKET t
JOIN REPORTING.DLAC_RESTRICTED.FACT_ZENDESK_TICKET_CONVERSATIONS_TECH_SUPPORT_AI c
    ON t.ID = c.TICKET_ID
WHERE t.ID = $ARGUMENTS
```

If the query returns no rows, tell the user the ticket wasn't found in Snowflake and stop.

### Step 2 — Fetch similar past tickets for context

```sql
SELECT
    candidate.TICKET_ID,
    candidate.PRIMARY_PRODUCT_COMPONENT,
    candidate.CUSTOMER_SITUATION,
    candidate.SUGGESTED_SOLUTION_BY_AGENTS,
    candidate.INVESTIGATION,
    VECTOR_COSINE_SIMILARITY(
        reference.EMBEDDINGS::VECTOR(FLOAT, 1536),
        candidate.EMBEDDINGS::VECTOR(FLOAT, 1536)
    ) AS similarity_score
FROM REPORTING.GENERAL.FACT_ZENDESK_TICKET_EMBEDDINGS reference
JOIN REPORTING.GENERAL.FACT_ZENDESK_TICKET_EMBEDDINGS candidate
    ON candidate.TICKET_ID != reference.TICKET_ID
WHERE reference.TICKET_ID = $ARGUMENTS
ORDER BY similarity_score DESC
LIMIT 5
```

If the ticket has no embedding yet (0 rows), skip this step silently and proceed to Step 3.

Use the results as background context when writing the KB article:
- If multiple similar tickets share the same resolution approach, that pattern is well-established — state it confidently in the Resolution section
- If similar tickets tried a fix that didn't fully work, note that as a caveat
- Do NOT cite ticket IDs or reference past tickets directly in the article — the reader doesn't have that context. Absorb the patterns and let them inform the writing.

### Step 3 — Generate the KB article

You are a technical knowledge base author. Write a standalone troubleshooting guide for anyone who encounters this problem in the future. The reader has never seen the ticket — write for them, not for the engineer who worked it.

Produce the article in the following structure:

---

**Title**
Pattern: `[Feature/Component] — [Specific problem in plain language]`
Good: "RUM Browser SDK — view.loading_time missing for initial_load views when third-party analytics are present"
Bad: "Dashboard issue" / a sentence-long ticket subject

**Problem**
2–4 sentences: what goes wrong, under what conditions, which product area and versions are affected. A reader should finish this section knowing whether this article is relevant to their case.

**Symptoms**
Bulleted checklist of observable indicators, most obvious to most diagnostic. Be concrete and verifiable:
- UI behaviours (error messages, loading states)
- Exact log/console errors in backticks
- API response codes and payloads
- Metric anomalies

**How to Troubleshoot**
Numbered steps an engineer follows in order. Each step must:
- State what to check or do (specifically — not "check the config", but which config, where, what value)
- State what result confirms this issue applies
- State what to conclude if the result is different (when to rule it out)

Goal: a decision tree. Follow the steps, confirm or rule out the issue.

**Resolution / Workaround**
Exact numbered steps. Separate permanent fixes from workarounds. For each:
- Specific commands, config changes, or UI actions
- Prerequisites or precautions
- How to verify it worked
- Scope of impact (downtime? affects other users?)

If no fix exists, say so explicitly. If a product fix is in progress, note the ticket/issue reference.

**Documentation Links**
Each link with a one-liner on why it's relevant.
Format: `[Page title](URL) — why it matters`
If a documentation gap exists, call it out explicitly: "No public documentation currently covers [X]."

---

### Rules
- Write in second person ("you"), addressing the engineer who will use this
- Never reference the original ticket, the customer, dates, or names
- Every claim must come from the ticket thread — do not invent troubleshooting steps
- If the root cause was uncertain in the ticket, reflect that honestly
- No padding — if there are two symptoms, list two
- A reader should be able to diagnose and resolve the issue using only this article

### Step 4 — Create a draft in the user's personal Confluence space

Once the KB article is generated, push it to Confluence as a personal draft:

1. Call `atlassianUserInfo` to get the current user's `accountId` and `cloudId`
2. Call `getConfluenceSpaces` with the filter `type=personal` to find the user's personal space — it will have a `key` matching `~<accountId>`. Use its `id` as the `spaceId`.
3. Call `createConfluencePage` with:
   - `cloudId` from step 1
   - `spaceId` from step 2
   - `status: "draft"` — keeps it unpublished until the user reviews and publishes it
   - `contentFormat: "html"`
   - `title`: the KB article title
   - `body`: the full KB article formatted as HTML, using:
     - `<h2>` for section headings (Problem, Symptoms, etc.)
     - `<ul>`/`<li>` for bullet lists
     - `<ol>`/`<li>` for numbered steps
     - `<pre><code>` for code blocks and inline config values
     - `<div data-type="panel-note"><p>...</p></div>` for the documentation gap callout
     - `<p>` for all body text

4. Return the direct URL to the draft page so the user can open it immediately. Format: `https://<site>.atlassian.net/wiki/spaces/~<accountId>/pages/<pageId>`

If `getConfluenceSpaces` returns no personal space, tell the user and offer to create the page in a space of their choosing instead.
