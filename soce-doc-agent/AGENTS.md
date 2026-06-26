# Agent Instructions

## Knowledge Store

`docs/solutions/` contains solution documentation for problems solved in this repo. Each file has YAML frontmatter (title, date, module, problem_type, tags, severity) and structured sections matching the problem type:

- `docs/solutions/design-patterns/` — reusable patterns for agent and pipeline design decisions

Before implementing a change that touches the KB generation pipeline (agent.py, prompts.py, static/app.js, static/index.html), search `docs/solutions/` for relevant prior work. Use the `tags` and `module` frontmatter fields to filter by area.

`CONCEPTS.md` at the repo root defines the authoritative vocabulary for this project. Use the terms there when discussing KB generation, the output contract, TEEs, and related pipeline concepts.

## Project Structure

- `agent.py` — KB generation agent (Anthropic API multi-step loop, tool dispatch, SSE streaming)
- `prompts.py` — system prompt and output contract definition
- `main.py` — FastAPI app, `/stream`, `/publish`, `/digest` endpoints
- `static/app.js` — frontend SSE client, draft preview, publish flow
- `static/index.html` — UI shell (Generate, Digest, Coverage tabs)
- `tools/` — tool implementations (algolia.py, snowflake.py)
- `docs/brainstorms/` — requirements documents from planning sessions
- `docs/plans/` — implementation plans (decision artifacts, not progress trackers)
- `docs/solutions/` — solution documentation for solved problems
