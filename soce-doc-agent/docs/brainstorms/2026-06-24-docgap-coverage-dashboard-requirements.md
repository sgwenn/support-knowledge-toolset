---
date: 2026-06-24
topic: docgap-coverage-dashboard
---

# DOC_GAP Coverage Dashboard — Requirements

## Summary

A Coverage Progress tab showing team-level KB documentation progress by product component. The backend persists digest run results and publish events in a local SQLite store; the tab renders a gap-vs-addressed breakdown per component and a weekly publish timeline. No Snowflake queries at dashboard load.

---

## Problem Frame

The support team has no visibility into which product components are well-documented in the internal Confluence KB. Documentation coverage is a blind spot — teams don't know if a component is covered, partially covered, or has no articles at all. The weekly digest already surfaces the highest-value undocumented tickets, but whether those candidates are ever addressed is untracked. This feature converts the tool's existing digest and publish events into a persistent, queryable coverage record.

---

## Key Decisions

**Gap data is scoped to digest-surfaced tickets, not the full Snowflake universe.** The digest already identifies the highest-value undocumented tickets using scoring and novelty checks. Coverage against that curated backlog is more meaningful than raw ticket count, and it eliminates the need for Snowflake queries when the dashboard loads.

**Coverage tracks publish events from this tool only.** The dashboard answers "what has soce-doc-agent documented?" — not "what exists in Confluence?" Articles created manually or through other workflows are invisible here. The tool's own output is the controlled variable, which makes progress measurement reliable.

**Component is sourced from ticket metadata and available in ticket-ID mode only.** Raw-text runs have no `primary_product_component` and are excluded from component-level coverage. The component field is already fetched from Snowflake during ticket-ID generation runs; it will be added to the `done` event so the frontend and persistence layer can record it.

---

## Requirements

**Data persistence**

- R1. When a digest run completes, the backend persists each returned ticket as a gap record: ticket ID, subject, component, score, and run timestamp.
- R2. When an article is published, the backend persists a coverage record: ticket ID (when available), component (from the preceding generate `done` event), Confluence page ID, title, and publish timestamp.
- R3. The `done` event from a generate run in ticket-ID mode includes the ticket's `primary_product_component`.
- R4. Generate runs in raw-text mode produce no component value and are excluded from component-level coverage metrics.
- R5. The SQLite store persists between server restarts.

**Coverage tab**

- R6. A Coverage tab is added to the main UI alongside the existing Generate and Digest tabs.
- R7. The Coverage tab displays a per-component breakdown: number of digest-surfaced gap records, number of addressed records, and a ratio expressed as a percentage.
- R8. A component with gap records and no matching addressed records is shown as fully unaddressed.
- R9. The Coverage tab displays a progress timeline: cumulative articles published per calendar week.
- R10. The Coverage tab loads from the local SQLite store and makes no Snowflake requests.

**Backend**

- R11. A `/coverage` GET endpoint returns the aggregated coverage data used by the tab: per-component gap and addressed counts, and the weekly publish timeline.

---

## Key Flows

- F1. **Digest run → gap records**
  - **Trigger:** User runs the digest (GET `/digest`).
  - **Steps:** Digest endpoint queries Snowflake, scores and returns the top tickets. Before returning the response, backend inserts each ticket into the gap records table.
  - **Outcome:** Coverage tab shows these tickets as unaddressed gaps for their respective components.

- F2. **Generate → publish → coverage record**
  - **Trigger:** Engineer generates a KB article from a ticket ID and then publishes it.
  - **Steps:** Generate completes; `done` event includes `primary_product_component`. Engineer publishes; publish endpoint inserts a coverage record with component and Confluence page ID.
  - **Outcome:** The component's addressed count increments in the Coverage tab.

---

## Acceptance Examples

- AE1. **Gap partially addressed**
  - **Covers:** R1, R2, R7, R8
  - **Given:** Digest surfaced 3 tickets for component "APM / Traces." Engineer publishes an article for one of them.
  - **When:** User opens the Coverage tab.
  - **Then:** "APM / Traces" shows 3 gap records, 1 addressed (33%).

- AE2. **Component with no digest history**
  - **Covers:** R7, R9
  - **Given:** Engineer publishes an article from a ticket ID never surfaced in any digest run (typed directly into the Generate tab).
  - **When:** User opens the Coverage tab.
  - **Then:** The article appears in the weekly publish timeline. The component shows no gap records and no ratio — the publish is visible but the backlog is undefined for that component.

---

## Scope Boundaries

**Deferred for later**

- Staleness detection — whether articles published through this tool remain accurate as the product evolves.
- Alerting when a component's addressed ratio falls below a threshold.
- User or team attribution on published articles.

**Outside this feature's scope**

- Confluence articles created manually or by other tools — the dashboard reflects soce-doc-agent output only.

---

## Dependencies / Assumptions

- `agent.py` currently does not return `primary_product_component` in the `done` event; a small change to the generate flow is required (grounding confirmed at `agent.py:177-195`).
- No SQLite ORM or persistence layer exists today; planning selects the approach.
- Digest run persistence is a new side effect on the `/digest` endpoint; the endpoint currently returns data to the frontend without writing anything to storage.

---

## Outstanding Questions

**Resolve before planning**

- Should coverage matching be by exact ticket ID (article for ticket #X closes the gap record for ticket #X) or by component (any article published for a component counts against that component's gap ratio)? Exact-ticket-ID matching is more precise; component matching is more forgiving for engineers who generate articles outside the digest flow.

**Deferred to planning**

- SQLite schema: table names, fields, indexes.
- Where to surface `primary_product_component` in the agent pipeline — whether to add it to `_parse_draft`'s return value or pass it through the task queue separately.

---

## Sources

- DOC_GAP extraction: `agent.py` (lines ~177–195, `_parse_draft`)
- Digest endpoint: `main.py` (lines ~165–203)
- Frontend tab structure: `static/index.html` (lines ~31–34), `static/app.js`
- No existing persistence layer confirmed: `requirements.txt`
