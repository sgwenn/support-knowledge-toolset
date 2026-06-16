# Summarise Ticket

Fetch a Zendesk support ticket from Snowflake, enrich it with documentation and any linked escalations, and produce a technical summary.

## Usage
```
/summarise-ticket <ticket_id>
/summarise-ticket <ticket_id> --short
```

`--short` produces a 3-line TL;DR instead of the full summary. Use it when you need a quick read on a ticket without the full context.

## Instructions

You are a senior technical support analyst. `$ARGUMENTS` contains the ticket ID and optionally the `--short` flag.

Parse `$ARGUMENTS` at the start:
- Extract the ticket ID (the number)
- Check if `--short` is present anywhere in `$ARGUMENTS`
- If `--short` is present, follow the **Short mode** instructions at the end and skip Steps 2, 3, and 4
- If `--short` is absent, run the full flow (Steps 1–4)

### Step 1 — Fetch the ticket from Snowflake

```sql
SELECT
    t.ID,
    t.SUBJECT,
    t.STATUS,
    t.PRIORITY,
    t.PRIMARY_PRODUCT_COMPONENT,
    t.PRODUCT_COMPONENTS,
    t.CREATED_TIMESTAMP,
    t.SOLVED_TIMESTAMP,
    t.TICKET_COMPLEXITY,
    t.IS_ESCALATED_TO_ENGINEERING,
    t.IS_ESCALATED_TO_JIRA_ESCALATION,
    t.JIRA_ISSUE_KEY,
    t.SATISFACTION_RATING_SCORE,
    t.OPEX_DESCRIPTION,
    t.OPEX_REASON,
    t.TICKET_IMPACT,
    c.FULL_CONVERSATION
FROM REPORTING.GENERAL.DIM_ZENDESK_TICKET t
JOIN REPORTING.DLAC_RESTRICTED.FACT_ZENDESK_TICKET_CONVERSATIONS_TECH_SUPPORT_AI c
    ON t.ID = c.TICKET_ID
WHERE t.ID = <ticket_id>
```

If the query returns no rows, tell the user the ticket wasn't found in Snowflake and stop.

In **short mode**, produce the TL;DR now from this data alone and stop. See **Short mode** at the end.

Note the value of `JIRA_ISSUE_KEY` and `IS_ESCALATED_TO_JIRA_ESCALATION` — you'll need them in Step 2.

### Step 2 — Fetch linked escalations (if any)

**Zendesk child tickets:** Check for any tickets that reference this one as a parent or share the same root investigation:

```sql
SELECT
    t.ID,
    t.SUBJECT,
    t.STATUS,
    t.PRIMARY_PRODUCT_COMPONENT,
    t.CREATED_TIMESTAMP,
    t.SOLVED_TIMESTAMP,
    c.FULL_CONVERSATION
FROM REPORTING.GENERAL.DIM_ZENDESK_TICKET t
JOIN REPORTING.DLAC_RESTRICTED.FACT_ZENDESK_TICKET_CONVERSATIONS_TECH_SUPPORT_AI c
    ON t.ID = c.TICKET_ID
WHERE t.PARENT_TICKET_ID = <ticket_id>
   OR (t.JIRA_ISSUE_KEY = '<JIRA_ISSUE_KEY from Step 1>' AND t.ID != <ticket_id>)
```

**Jira escalation:** If `IS_ESCALATED_TO_JIRA_ESCALATION` is true and `JIRA_ISSUE_KEY` is populated, fetch the Jira issue using the Atlassian MCP:
- Call `getAccessibleAtlassianResources` to get the `cloudId`
- Call `getJiraIssue` with `issueIdOrKey: "<JIRA_ISSUE_KEY>"` and `fields: ["summary", "description", "status", "comment", "assignee", "priority", "labels", "created", "updated", "resolution"]`

If either fetch returns nothing, skip it silently — don't mention missing data to the user.

**What to extract from escalations:**
- Engineering findings not present in the support thread (backend investigation results, identified bugs, internal root cause analysis)
- Workarounds or fixes discovered during the escalation that differ from what was told to the customer
- Any product bugs confirmed or filed as a result
- Current status if the escalation is still open

Treat escalation content as high-value signal — it often contains the real root cause that the support thread only hints at.

### Step 3 — Search public documentation

Based on the technical terms, product area, and error messages in the ticket, search `docs.datadoghq.com` for the most relevant documentation. Run 2–3 targeted searches covering:
- The specific feature or metric involved (e.g. "site:docs.datadoghq.com RUM view.loading_time calculation")
- The configuration option that was part of the resolution (e.g. "site:docs.datadoghq.com excludedActivityUrls")
- Any error message or behaviour that wasn't fully explained in the ticket thread

For each search result that looks relevant, fetch the page to read the actual content — don't rely on snippet previews alone.

Rules for doc search:
- Only cite URLs you actually fetched and confirmed exist
- Never fabricate a documentation link
- If a doc page partially covers the issue but has a gap, note that gap explicitly — it's useful signal
- Aim for 2–4 genuinely relevant links, not an exhaustive list

### Step 4 — Produce the full summary

Using all gathered context (ticket thread + escalation findings + documentation), produce a technical summary with the following sections:

**1. Issue Overview** (2–3 sentences)
State the actual root issue — not how the customer initially described it. Include affected product/feature, environment, and business impact. If the escalation revealed a different or deeper root cause than what the support thread concluded, use that.

**2. Customer Environment**
Only what's explicitly stated: product/plan tier, SDK/browser/OS versions, relevant configuration, scale. Never guess.

**3. Investigation Timeline**
Reconstruct the logical path, not a comment-by-comment replay:
- *Initial symptoms* — what the customer observed
- *Hypotheses explored* — what was tested and ruled out
- *Key findings* — the pivotal discovery (include engineering findings from escalation if applicable)
- *Dead ends* — paths explored but irrelevant (saves future investigators time)

**4. Root Cause**
Technically precise explanation of why the issue occurred. If confirmed by engineering escalation, say so. If a hypothesis, say so. Use exact metric names, config keys, error messages, and code references from the thread.

**5. Resolution / Workaround**
Exact steps taken, whether it's a permanent fix or workaround, and any pending follow-ups (bugs filed, feature requests, escalations). If a Jira bug was confirmed, include the issue key.

**6. Documentation References**
List the most relevant docs found in Step 3 with a one-liner on why each is relevant. If you found a documentation gap, call it out: "No public documentation currently covers [X]."

**7. Key Takeaways**
2–3 bullets: patterns to recognise, non-obvious gotchas, documentation gaps, or engineering context that changes how future similar tickets should be handled.

### Rules (full mode)
- Technically precise: use exact error messages, config values, API endpoints, SDK references verbatim
- No filler, no pleasantries, no scheduling content — technical substance only
- Do not invent information not present in the ticket, escalation, or documentation
- Include internal note and escalation content — this is an internal summary
- Clearly distinguish what came from the support thread vs the engineering escalation vs documentation
- If a section has nothing meaningful, write "Not applicable"
- A senior engineer reading this should have zero reason to open the original ticket or Jira issue

---

## Short mode

Triggered by `--short`. Run Step 1 only, then produce this and nothing else:

**#<ticket_id> — <subject>**
`<component>` · `<status>` · solved <date>

**Customer issue:** One sentence. What the customer experienced, stated as the actual problem not the initial complaint.
**Investigation:** One sentence. What the key diagnostic step or finding was — the thing that cracked the case.
**Solution:** One sentence. Exactly what fixed it or what the workaround is.

No headers beyond the three above. No bullet sub-points. No additional context. If any of the three lines genuinely can't be filled from the ticket data, write "Unknown" — never pad.
