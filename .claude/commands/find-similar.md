# Find Similar Tickets

Find the most similar past tickets to a given ticket using vector similarity search.

## Usage
```
/find-similar <ticket_id>
```

## Instructions

The user has provided a Zendesk ticket ID as `$ARGUMENTS`.

### Step 1 — Fetch the reference ticket metadata

```sql
SELECT
    t.ID,
    t.SUBJECT,
    t.STATUS,
    t.PRIMARY_PRODUCT_COMPONENT,
    t.CREATED_TIMESTAMP,
    t.SOLVED_TIMESTAMP,
    e.CUSTOMER_SITUATION,
    e.SUMMARY
FROM REPORTING.GENERAL.DIM_ZENDESK_TICKET t
JOIN REPORTING.GENERAL.FACT_ZENDESK_TICKET_EMBEDDINGS e
    ON t.ID = e.TICKET_ID
WHERE t.ID = $ARGUMENTS
```

If no rows are returned, tell the user the ticket wasn't found in the embeddings table and stop.

### Step 2 — Find the top 8 similar tickets

```sql
SELECT
    candidate.TICKET_ID,
    t.SUBJECT,
    t.STATUS,
    t.SOLVED_TIMESTAMP,
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
JOIN REPORTING.GENERAL.DIM_ZENDESK_TICKET t
    ON candidate.TICKET_ID = t.ID
WHERE reference.TICKET_ID = $ARGUMENTS
ORDER BY similarity_score DESC
LIMIT 8
```

### Step 3 — Present the results

Show a brief header with the reference ticket subject and product component, then present the similar tickets grouped into two tiers:

**Strong matches** (similarity ≥ 0.89): These are highly likely to be the same root cause. Lead with these.
**Related** (similarity 0.85–0.89): Same product area or pattern, worth reviewing.

For each ticket show:
- Ticket ID as a Zendesk link: `https://datadog.zendesk.com/agent/tickets/<TICKET_ID>`
- Similarity score (2 decimal places as a percentage, e.g. 89%)
- Product component
- One-line situation summary (paraphrase `CUSTOMER_SITUATION`, keep it under 20 words)
- The suggested solution in 1–2 sentences (from `SUGGESTED_SOLUTION_BY_AGENTS`)
- Status and solved date if resolved

End with a "Patterns across these tickets" section: look across all the results and call out any solution approaches that appear in multiple tickets (e.g. `excludedActivityUrls` appearing in 4/8 results). This is the highest-value part — it tells the engineer what the proven fix is before they've written a single reply.
