# soce-doc-agent

A FastAPI server that automatically generates Confluence KB article drafts from Zendesk support tickets using the Claude API.

This is a companion service to the Claude slash commands in this repo. Where the slash commands let you summarize or draft KB content interactively inside Claude, soce-doc-agent runs as a persistent web server with a browser UI, a server-side multi-turn Claude agent loop, and direct Confluence integration — so articles can be generated and published without leaving a browser tab.

## What it does

1. Accepts a Zendesk ticket ID (or raw text) via a browser UI or REST API
2. Fetches ticket metadata from Snowflake, checks Algolia for existing docs coverage, and searches Confluence for related articles
3. Runs a multi-turn Claude loop that produces a structured KB article draft (title + HTML body)
4. Streams progress to the browser in real time via SSE
5. Publishes the draft to Confluence under the correct product component page with one click

## Relationship to the slash commands

| Slash commands (this repo) | soce-doc-agent |
|---------------------------|----------------|
| Interactive, Claude-native | Standalone server |
| One ticket at a time, manually | Batch-ready via `/digest` endpoint |
| No Confluence write needed | Full publish + OAuth flow |
| Great for ad-hoc drafting | Great for systematic gap coverage |

## Setup

```bash
cp .env.example .env
# Fill in required values (see Environment Variables below)

pip install -r requirements.txt
uvicorn main:app --reload
```

Open `http://localhost:8000` in a browser and connect Confluence via the OAuth button.

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `SESSION_SECRET` | Yes | Random string for signed session cookies — generate with `python -c "import secrets; print(secrets.token_hex(32))"` |
| `ANTHROPIC_API_KEY` | Yes | Claude API key |
| `ATLASSIAN_CLIENT_ID` | Yes (OAuth) | Atlassian OAuth app client ID |
| `ATLASSIAN_CLIENT_SECRET` | Yes (OAuth) | Atlassian OAuth app client secret |
| `ATLASSIAN_REDIRECT_URI` | No | OAuth callback URL (default: `http://localhost:8000/auth/callback`) |
| `ATLASSIAN_API_TOKEN` | Optional | Personal Access Token — enables draft pages owned by you (resumable edit URLs) |
| `ATLASSIAN_USER_EMAIL` | Optional | Required alongside `ATLASSIAN_API_TOKEN` |
| `SNOWFLAKE_ACCOUNT` | Yes | Snowflake account identifier |
| `SNOWFLAKE_USER` | Yes | Snowflake username |
| `SNOWFLAKE_PASSWORD` | Yes | Snowflake password |
| `SNOWFLAKE_WAREHOUSE` | Yes | Snowflake warehouse |
| `SNOWFLAKE_DATABASE` | Yes | Snowflake database |
| `ALGOLIA_APP_ID` | Optional | Algolia application ID for docs search |
| `ALGOLIA_API_KEY` | Optional | Algolia search API key |

## Architecture

`main.py` accepts a ticket ID or raw text on `POST /run`, creates a job, and streams progress via SSE on `GET /stream/{job_id}`. `agent.py` drives a multi-turn Claude API loop: it pre-fetches Jira context if the ticket is escalated, then iterates until Claude emits `end_turn`. All tool calls in a single turn execute concurrently. The output contract requires Claude to end with `DRAFT_TITLE:`, `EXISTING_URL:`, and `DRAFT_HTML:` markers. `POST /publish` triggers a publish agent that creates or updates the Confluence page under the right product area.

## Running tests

```bash
pip install -r requirements-dev.txt
pytest
```

## API endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/run` | POST | Submit a ticket ID or text, returns `job_id` |
| `/stream/{job_id}` | GET | SSE stream of agent progress and final draft |
| `/publish` | POST | Publish a draft to Confluence |
| `/digest` | POST | Batch run for gap coverage (multiple tickets) |
| `/coverage` | GET | Coverage stats by product component |
| `/auth/confluence` | GET | Start Confluence OAuth flow |
| `/auth/callback` | GET | OAuth callback |
| `/auth/status` | GET | Check Confluence connection status |
