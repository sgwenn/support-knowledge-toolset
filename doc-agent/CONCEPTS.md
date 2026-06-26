# Concepts

Core domain vocabulary for soce-doc-agent. Terms here have precise meaning in this codebase that a new engineer would need defined to follow conversations, code, and tickets.

---

## KB Generation Pipeline

### Ticket
A Zendesk support ticket submitted by a customer, used as the primary input to the KB generation pipeline. The agent reads the ticket's description, resolution steps, and metadata to draft a KB article.

### KB Article
A Confluence knowledge base article documenting the resolution to a support problem. The primary artifact produced by the pipeline. Drafted by the agent and published by the TEE after review.

### TEE (Technical Escalation Engineer)
The primary user of the system. TEEs investigate complex support tickets, use the agent to draft KB articles from resolved cases, and publish articles to Confluence. TEEs operate under high context-switching load and are not expected to deeply read the agent's intermediate reasoning output.
*Avoid:* support engineer, SE (too general)

### Output Contract
The set of structured markers the agent must emit in its final text response, defining what fields the SSE pipeline will extract and deliver to the frontend. The current output contract is three markers: `DRAFT_TITLE`, `EXISTING_URL`, and `DRAFT_HTML`. Changing the output contract requires coordinated changes across the prompt, the parser, the done event, and the frontend.

### Done Event
The SSE event emitted when KB generation completes, carrying the fields extracted from the agent's output contract. The frontend's article preview and publish flow are driven entirely by the done event payload.

---

## Patterns

### Verdict Surface
A UI element that renders an agent's intermediate classification output — a label, badge, or bar representing the agent's judgment on some axis — as a visible, user-facing result separate from the primary artifact. Verdict surfaces can be removed without removing the underlying tool call that produces the classification.

### Silent Context Tool
A tool registered in the agent's tool list whose output is consumed internally to improve the primary artifact, without being rendered as a separate verdict for the user. The `search_docs` Algolia tool operates as a silent context tool after the DOC_GAP verdict removal: the agent still calls it, and the results inform how the KB article is drafted, but no classification is shown to the TEE.

---

## Flagged Ambiguities

- "gap" was used both as a general English word ("coverage gap") and as the specific `GAP` verdict in the `DOC_GAP` output marker — these are distinct. The verdict was removed; the general concept of incomplete documentation coverage remains valid.
