# Weekly KB Digest

Surface the top 5 tickets from the past week most worth turning into KB articles, ranked by knowledge value.

## Usage
```
/weekly-digest
```

Optionally scope to a product component:
```
/weekly-digest rum_ppc
```

## Instructions

You are a support knowledge manager. Your job is to scan last week's solved tickets and identify the ones with the highest knowledge value — the investigations that, if documented, would most help the team handle future tickets faster.

`$ARGUMENTS` is an optional product component filter (e.g. `rum_ppc`, `apm_ppc`, `logs_ppc`). If empty, scan all components.

### Step 1 — Fetch candidate tickets from last week

```sql
SELECT
    t.ID,
    t.SUBJECT,
    t.STATUS,
    t.PRIMARY_PRODUCT_COMPONENT,
    t.TICKET_COMPLEXITY,
    t.IS_ESCALATED_TO_ENGINEERING,
    t.IS_ESCALATED_TO_JIRA_ESCALATION,
    t.JIRA_ISSUE_KEY,
    t.TICKET_IMPACT,
    t.OPEX_DESCRIPTION,
    t.OPEX_REASON,
    t.SATISFACTION_RATING_SCORE,
    t.SOLVED_TIMESTAMP,
    e.SUMMARY,
    e.CUSTOMER_SITUATION,
    e.INVESTIGATION,
    e.SUGGESTED_SOLUTION_BY_AGENTS
FROM REPORTING.GENERAL.DIM_ZENDESK_TICKET t
JOIN REPORTING.GENERAL.FACT_ZENDESK_TICKET_EMBEDDINGS e
    ON t.ID = e.TICKET_ID
WHERE t.SOLVED_TIMESTAMP >= DATEADD(day, -7, CURRENT_TIMESTAMP())
    AND t.STATUS IN ('closed', 'solved')
    AND t.PRIMARY_PRODUCT_COMPONENT IS NOT NULL
    AND e.SUMMARY IS NOT NULL
```

Add this clause if `$ARGUMENTS` is provided:
```sql
    AND t.PRIMARY_PRODUCT_COMPONENT = '$ARGUMENTS'
```

If the query returns 0 rows, tell the user no solved tickets with embeddings were found for that period and stop.

### Step 2 — Score each ticket for knowledge value

For each ticket returned, compute a knowledge value score using this logic. You are looking for tickets where a KB article would genuinely help a future engineer — either because the diagnosis path was non-obvious, the solution was a clever workaround, or the behaviour being explained is subtle. Product bugs and feature gaps are still worth surfacing, but a well-investigated configuration issue is equally valuable.

Score each ticket across two independent dimensions, then combine them.

**Dimension A — Investigation quality (0–6 points)**
This is the primary signal. Score from the `INVESTIGATION` and `SUGGESTED_SOLUTION_BY_AGENTS` fields:

| Signal | Points | What to look for |
|---|---|---|
| Non-obvious diagnosis | +2 | Multiple hypotheses explored, dead ends documented, root cause required reading source code / backend data / non-standard tooling |
| Non-obvious solution | +2 | Fix is a specific configuration, an SDK parameter, a workaround for a product behaviour — not "enable the feature" or "check the docs" |
| Transferable reasoning | +2 | The `INVESTIGATION` field reads like something an engineer could learn from — the logic is explained, not just the answer. Future engineers could follow the same path. |

Apply -2 if the investigation is trivial end-to-end (e.g. wrong API key, customer hadn't read the docs, straightforward misconfiguration with no diagnostic work needed). These are not worth documenting.

**Dimension B — Recurrence and impact signals (0–4 points)**
These are supporting signals only — they adjust priority but do not override investigation quality.

| Signal | Points | How to detect |
|---|---|---|
| High complexity | +2 | `TICKET_COMPLEXITY = 'high'` |
| High/critical impact | +1 | `TICKET_IMPACT IN ('high', 'critical')` |
| Jira or eng escalation | +1 | `IS_ESCALATED_TO_JIRA_ESCALATION = true` OR `IS_ESCALATED_TO_ENGINEERING = true` |

Note: OPEX and bad CSAT are intentionally excluded from scoring. They signal product gaps and customer frustration, not investigation quality. A ticket can be OPEX-flagged because a feature is missing — that's a product problem, not necessarily a knowledge gap worth a KB article. These fields are still shown as tags in the output so leads can see them, but they don't influence ranking.

**Final score = Dimension A + Dimension B (max 10)**

A ticket with a genuinely clever non-obvious workaround should score 6+ even with no escalation flags. A Jira-escalated bug with a trivial investigation path should score no higher than 3.

### Step 3 — Check novelty against embeddings

For the top 10 candidates by score, run one query per candidate to find its single closest past neighbour, scoped to the same product component. Scoping to the component cuts the comparison set from ~683k rows to ~5–20k, which makes this fast enough to run in the digest without meaningful quality loss — cross-component duplicates are rare in practice.

Run this once per candidate ticket, substituting its `TICKET_ID` and `PRIMARY_PRODUCT_COMPONENT`:

```sql
SELECT
    other.TICKET_ID AS nearest_neighbour_id,
    VECTOR_COSINE_SIMILARITY(
        candidate.EMBEDDINGS::VECTOR(FLOAT, 1536),
        other.EMBEDDINGS::VECTOR(FLOAT, 1536)
    ) AS similarity
FROM REPORTING.GENERAL.FACT_ZENDESK_TICKET_EMBEDDINGS candidate
JOIN REPORTING.GENERAL.FACT_ZENDESK_TICKET_EMBEDDINGS other
    ON other.TICKET_ID != candidate.TICKET_ID
    AND other.PRIMARY_PRODUCT_COMPONENT = candidate.PRIMARY_PRODUCT_COMPONENT
    AND other.CREATED_TIMESTAMP < DATEADD(day, -7, CURRENT_TIMESTAMP())
WHERE candidate.TICKET_ID = <candidate_ticket_id>
ORDER BY similarity DESC
LIMIT 1
```

This returns the single most similar past ticket within the same component. If even the closest match is below the threshold, the ticket is novel — no need to scan further.

Apply the novelty adjustment based on the returned `similarity` value:
- `similarity < 0.82` → genuinely novel, +2 points, tag as "Novel issue"
- `similarity 0.82–0.88` → variation on a known pattern, +0 points
- `similarity > 0.88` → well-covered territory, -1 point (deprioritise unless investigation quality score is very strong)

Run all 10 queries in parallel if possible; otherwise run sequentially. In either case this is substantially faster than the previous full-table GROUP BY MAX scan.

### Step 4 — Select and rank the top 5

Take the 5 highest-scoring tickets after novelty adjustment. If two tickets are tied, prefer the one with the higher Dimension A (investigation quality) score — a better-investigated ticket is more valuable to document than one that just has more escalation flags.

For each of the 5 tickets, prepare:
- A one-paragraph "why this is worth documenting" explanation (3–5 sentences) written for a support lead. Be specific: name the actual root cause or behaviour, explain what made the diagnosis non-obvious, and say concretely what a KB article would help a future engineer avoid. Draw from `INVESTIGATION` and `SUGGESTED_SOLUTION_BY_AGENTS`. Never be generic ("this was complex").
- A tag set from: `Non-obvious fix`, `Clever workaround`, `Multi-hypothesis investigation`, `Novel issue`, `Recurring pattern`, `Seasonal pattern`, `Jira escalation`, `Eng escalation`, `OPEX flagged` (display only — shown for context, not a ranking factor)
- A value rating: "High value" (score ≥ 7) or "Medium value" (score 4–6)

### Step 5 — Present the digest

Output a digest in this format:

---

**Weekly KB digest — [date range]**
[N] tickets solved · [N] high complexity · [N] escalated to engineering · [N] novel issues

---

For each of the 5 tickets:

**#[rank] [ticket subject]**
`#[ticket_id]` · solved [date] · `[component]`
[Tags: Jira escalation · High complexity · etc.]
[Value: High / Medium]

> [Why it's worth documenting — 3-5 sentences, specific and technical]

Actions: `summarise ticket [id]` · `generate kb [id]`

---

After the list, add a one-paragraph **Patterns this week** section: look across all 5 tickets and call out any themes — e.g. "three of this week's top tickets involve SDK upgrade regressions" or "two involve undocumented limits that customers are hitting at scale." This is the signal a support lead needs to decide whether to prioritise a documentation sprint or flag something to engineering.

### Rules

- The "why it's worth documenting" blurb must be specific to the actual ticket content — never generic ("this was a complex investigation"). Name the actual root cause, the actual fix, the actual product behaviour.
- If all top tickets are from one component and `$ARGUMENTS` was not set, note that at the top — it may indicate a product area having a difficult week.
- Do not include tickets where the investigation is trivially documented ("customer needed to enable the feature flag") unless no better candidates exist.
- If fewer than 5 qualifying tickets exist (e.g. filtered by component), return however many qualify and say so.
