# Support Knowledge Toolset — Setup Guide

A set of Claude slash commands that turn Zendesk ticket investigations into structured knowledge: summaries, KB articles, similar ticket search, and a weekly digest of what's worth documenting.

---

## Setup

### Option A — Claude Project (recommended, zero setup)

1. Go to [claude.ai](https://claude.ai) and create a new Project
2. Open Project Settings → Instructions
3. Paste the contents of `PROJECT_INSTRUCTIONS.md` into the instructions field
4. Under Integrations, connect the **Snowflake** and **Atlassian** MCPs
5. Share the project with your team

Anyone in the project can then type naturally — "summarise ticket 2440781", "generate a KB for ticket 2440781", "run the weekly digest" — no command syntax needed.

### Option B — Claude Code / Cursor slash commands

Copy the `.claude/` folder into the root of any repo your team works from:

```
your-repo/
└── .claude/
    └── commands/
        ├── summarise-ticket.md
        ├── generate-kb.md
        ├── find-similar.md
        ├── investigate-jira.md
        └── weekly-digest.md
```

Claude Code and Cursor automatically pick up `.md` files in `.claude/commands/` as slash commands. Commit the folder so everyone on the team gets them on clone.

**MCP configuration (one-time per machine):**
```bash
claude mcp add snowflake --url https://kn27690-sza96462.snowflakecomputing.com/api/v2/databases/reporting/schemas/general/mcp-servers/CLAUDE_MCP_SERVER
claude mcp add atlassian --url https://mcp.atlassian.com/v1/mcp
```

---

## Commands

### `/summarise-ticket <ticket_id>`
Fetches the ticket from Snowflake, checks for linked Jira or Zendesk escalations, searches `docs.datadoghq.com` for relevant documentation, and produces a 7-section internal technical summary:

1. Issue overview
2. Customer environment
3. Investigation timeline (including dead ends)
4. Root cause
5. Resolution / workaround
6. Documentation references
7. Key takeaways

Escalation findings are clearly labelled as coming from engineering. Documentation gaps are called out explicitly.

**Short mode** — for a quick read without the full context:
```
/summarise-ticket <ticket_id> --short
```
Returns three lines only:
- **Customer issue** — what the customer experienced, stated as the actual problem
- **Investigation** — the key diagnostic step or finding that cracked the case
- **Solution** — exactly what fixed it or what the workaround is

No escalation fetch, no doc search. Fast by design.

---

### `/generate-kb <ticket_id>`
Fetches the ticket, checks for linked escalations, runs vector similarity search for context from past similar tickets, searches public docs, and generates a Confluence-ready KB article:

- **Title** — searchable, pattern: `[Feature/Component] — [specific problem]`
- **Problem** — what goes wrong and under what conditions
- **Symptoms** — concrete verifiable checklist (exact error text in backticks)
- **How to troubleshoot** — numbered decision tree, each step with expected result and how to rule it out
- **Resolution / workaround** — exact steps, how to verify it worked, scope of impact
- **Documentation links** — with relevance notes; gaps called out explicitly

After generating the article, pushes it as a draft to your personal Confluence space via the Atlassian MCP. Returns the direct draft URL.

---

### `/find-similar <ticket_id>`
Uses vector cosine similarity on `FACT_ZENDESK_TICKET_EMBEDDINGS` to find the 8 most similar past tickets across all product components. Results are grouped into strong matches (≥89% similarity) and related (85–89%).

Ends with a **Patterns across these tickets** section that calls out solution approaches appearing in multiple results — the proven fix before you've written a single reply.

---

### `/investigate-jira <issue_key>`
Starts from a Jira issue key instead of a ticket ID. Fetches the Jira issue via the Atlassian MCP, finds all linked Zendesk tickets in Snowflake, searches public docs, and synthesises across all sources into a structured output:

- Issue overview from an engineering perspective
- Scope (number of linked tickets, affected components, engineering status)
- Engineering investigation (what was found, ruled out, confirmed)
- Customer impact (synthesised from Zendesk conversations)
- Current workaround / resolution
- Documentation references
- Key takeaways for support engineers handling related tickets

After producing the summary, asks whether to generate a KB article and push it to Confluence as a draft.

If Jira and Zendesk tell different stories about the root cause, surfaces the discrepancy rather than silently picking one.

---

### `/weekly-digest [component]`
Scans all tickets solved in the past 7 days and surfaces the top 5 most worth turning into KB articles, ranked by knowledge value.

**Scoring is intentionally investigation-quality-first**, not product-gap-first. OPEX flags and Jira escalations are displayed as context but do not dominate the ranking — a ticket with a non-obvious workaround and a smart diagnosis path scores higher than a straightforward bug escalation.

Scoring uses two dimensions:
- **Investigation quality (0–6 pts):** non-obvious diagnosis, non-obvious solution, transferable reasoning
- **Recurrence and impact signals (0–4 pts):** complexity, impact level, escalation flags

Novelty is checked via embeddings similarity — genuinely novel issues (low similarity to all prior tickets) get a bonus; well-covered territory is deprioritised.

Each result includes a specific "why this is worth documenting" explanation and a **Patterns this week** section at the end calling out themes across the top 5.

Optionally scope to one product component:
```
/weekly-digest rum_ppc
```

---

## Data sources

| Table | Used for |
|---|---|
| `REPORTING.GENERAL.DIM_ZENDESK_TICKET` | Ticket metadata: status, priority, component, complexity, escalation flags, OPEX, impact, CSAT |
| `REPORTING.DLAC_RESTRICTED.FACT_ZENDESK_TICKET_CONVERSATIONS_TECH_SUPPORT_AI` | Full conversation threads. Current to ~1 week lag. Use this, not `CONVERSATIONS_WITH_TICKET_INFO` (stale since Jul 2025) |
| `REPORTING.GENERAL.FACT_ZENDESK_TICKET_EMBEDDINGS` | Pre-computed 1536-dim embeddings (text-embedding-ada-002) + structured fields: `SUMMARY`, `CUSTOMER_SITUATION`, `INVESTIGATION`, `SUGGESTED_SOLUTION_BY_AGENTS` |

---

## Improving output quality

The single highest-leverage thing you can do: take a summary or KB article you've hand-edited to be exactly right, and paste it into the relevant command file as a few-shot example. Even one good example significantly improves output consistency.

For the weekly digest specifically, the similarity thresholds (0.82 / 0.88) and scoring weights are starting points — tune them after a few weeks of real output to match your team's sense of what's actually worth documenting.
