# soce-doc-agent — Claude Code Guide

## Navigating this codebase

**Before grepping or reading a whole file, check `code-index.json`** (generated artifact at repo root):

- **Find a function or class by name** → look up `symbols["name"]` → get `file` + `line`, then read only that range
- **See what a file contains** → read `files["path"]` for its summary, functions, and classes
- **Regenerate after code changes** → `python scripts/index_codebase.py`
- **Note:** `code-index.json` is gitignored (generated artifact). If it's absent, regenerate it first with `python scripts/index_codebase.py` before navigating.

## Key entry points

| File | Purpose |
|------|---------|
| `agent.py` | Claude API loop, tool dispatch (`_dispatch_tool`), SSE event emission |
| `main.py` | FastAPI routes: `/run` `/stream` `/publish` `/digest` `/coverage` `/auth/confluence` `/auth/callback` `/auth/status` |
| `prompts.py` | System prompt and output contract (3 markers: `DRAFT_TITLE`, `EXISTING_URL`, `DRAFT_HTML`) |
| `tools/snowflake.py` | Ticket fetch, vector cosine similarity, novelty scoring, digest candidates |
| `tools/algolia.py` | Public docs search — silent context tool (results inform drafting, not surfaced as verdict) |
| `tools/confluence.py` | CQL search, page creation/update, OAuth2 token refresh, component aliasing |
| `db.py` | SQLite schema for coverage/gap tracking |

## Architecture in one paragraph

`main.py` accepts a ticket ID or raw text on `POST /run`, creates a job, and streams progress via SSE on `GET /stream/{job_id}`. `agent.py:run_kb_agent()` drives a multi-turn Claude API loop: it pre-fetches Jira context if the ticket is escalated, then iterates until Claude emits `end_turn`. All tool calls in a single turn execute concurrently via `asyncio.gather()`. The output contract requires Claude to end with `DRAFT_TITLE:`, `EXISTING_URL:`, and `DRAFT_HTML:` markers. `POST /publish` triggers `run_publish_agent()` to create or update the Confluence page.

## Patterns to know

- **Silent context tool** — `search_docs` (Algolia) is called and its results inform the draft, but no verdict badge is surfaced to users. See `docs/solutions/design-patterns/hide-verdict-surface-keep-tool-active.md`.
- **Component aliasing** — `COMPONENT_ALIASES` in `confluence.py` maps Snowflake product names to Confluence TS space page titles.
- **OAuth2 rotation** — Confluence tokens auto-refresh proactively (60s before expiry via `_token_lock` in `confluence.py`) and are persisted to the SQLite `oauth_tokens` table — not `.env`. The lock is intentionally held across the HTTP refresh call to prevent double-refresh with Atlassian's rotating token scheme.
- **Novelty scoring** — `batch_novelty_check()` in `snowflake.py` uses `VECTOR_COSINE_SIMILARITY` to skip generating KB articles for already-covered topics.
- **SESSION_SECRET** — Required at startup (hard RuntimeError if absent) for OAuth state CSRF protection. Generate with: `python -c "import secrets; print(secrets.token_hex(32))"`. Also needed: `ATLASSIAN_CLIENT_ID`, `ATLASSIAN_CLIENT_SECRET`, and optionally `ATLASSIAN_REDIRECT_URI` (defaults to `http://localhost:8000/auth/callback`).
