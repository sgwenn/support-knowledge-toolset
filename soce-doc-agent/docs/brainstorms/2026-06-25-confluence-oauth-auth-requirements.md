# Confluence OAuth Core Auth Rewrite — Requirements

**Date:** 2026-06-25
**Source:** [docs/ideation/2026-06-25-confluence-oauth-auth-ideation.html](../ideation/2026-06-25-confluence-oauth-auth-ideation.html) (Ideas 1, 2, 3)

---

## Problem

The Atlassian OAuth flow runs as a standalone CLI script (`scripts/atlassian_oauth.py`). Tokens are stored in `.env` as plaintext. Token refresh fires only on 401. Three failure modes result:

1. **Manual bootstrap** — dev must run a terminal script, copy stdout, paste into `.env`, restart the server — 5 steps every time tokens expire or the environment resets.
2. **Permanent lockout** — Atlassian rotating refresh tokens mean a crash (or process kill) between the token exchange response and the `.env` write permanently loses the new refresh token. The old token is already invalidated. Re-auth requires going through the full flow again manually.
3. **Mid-job 401s** — no expiry tracking means the access token (~3600s TTL) may expire during a generation run, causing Confluence 404s mid-stream.

---

## Scope

**In scope (this doc):** Ideas 1 (In-App OAuth Routes), 2 (SQLite Token Store), 3 (Proactive Pre-Refresh).

**Out of scope:** Ideas 4 (Confluence status indicator widget) and 5 (reconnect CTA on 401 error) — these build on this rewrite and are tracked for a follow-up. PAT fallback (`create_draft_page_pat`) is left untouched.

---

## Functional Requirements

### Idea 1 — In-App OAuth Routes

**FR-1.1** `main.py` exposes `GET /auth/confluence`. This route generates a PKCE code verifier + state nonce, stores both in the Starlette session cookie, then issues an HTTP redirect to the Atlassian authorization URL with the appropriate scopes.

**FR-1.2** `main.py` exposes `GET /auth/callback`. This route:
1. Validates the returned `state` against the session (reject mismatches with 400).
2. Exchanges the `code` for access token + refresh token + `expires_in` via the Atlassian token endpoint.
3. Fetches `cloud_id` from the Atlassian accessible-resources endpoint using the new access token.
4. Writes all tokens atomically to the `oauth_tokens` SQLite table (FR-2.1).
5. Redirects to `/`.

**FR-1.3** `main.py` adds `SessionMiddleware` (Starlette, already bundled with FastAPI). The session secret is read from `SESSION_SECRET` in the environment. If unset on startup, the app logs a warning and falls back to a random secret — sessions survive only until the next restart in this mode.

**FR-1.4** `main.py` exposes `GET /auth/status`. Returns JSON:
```json
{"confluence_connected": true}
```
`confluence_connected` is `true` when the `oauth_tokens` table has a non-expired `atlassian` row (i.e., `expires_at` is null or `> now`). This endpoint is called by the frontend on page load to conditionally render the connection button.

**FR-1.5** `static/index.html` adds a "Connect Confluence" button to the header. On page load, `app.js` calls `GET /auth/status` and:
- If not connected: shows the button ("Connect Confluence") linking to `/auth/confluence`.
- If connected: shows a muted "Confluence ✓" indicator in its place.

**FR-1.6** The callback URL registered in the Atlassian developer console must be updated to `http://localhost:8000/auth/callback` (one-time setup, documented in README / `.env.example`). The existing standalone script (`scripts/atlassian_oauth.py`) is marked deprecated with a comment but not deleted.

---

### Idea 2 — SQLite Token Store

**FR-2.1** `db.py` adds an `oauth_tokens` table:

```sql
CREATE TABLE IF NOT EXISTS oauth_tokens (
    service       TEXT PRIMARY KEY,
    access_token  TEXT NOT NULL,
    refresh_token TEXT,
    expires_at    INTEGER,      -- Unix timestamp, UTC
    cloud_id      TEXT,
    client_id     TEXT,
    client_secret TEXT,
    updated_at    INTEGER
);
```

`service = 'atlassian'` is the only row used today. The `PRIMARY KEY` on `service` makes upserts atomic via `INSERT OR REPLACE`.

**FR-2.2** `db.py` exposes two helpers used by `confluence.py` and the auth routes:
- `get_atlassian_tokens() -> dict | None` — reads the `atlassian` row; returns `None` if absent.
- `save_atlassian_tokens(access_token, refresh_token, expires_at, cloud_id, client_id, client_secret)` — executes an `INSERT OR REPLACE` in a single transaction.

**FR-2.3** `_get_token()` in `confluence.py` (currently `confluence.py:108`) is replaced: it calls `get_atlassian_tokens()` and returns `access_token`. It no longer reads `os.environ["ATLASSIAN_OAUTH_TOKEN"]`.

**FR-2.4** `_persist_env_tokens()` in `confluence.py` (currently `confluence.py:41`) is replaced by `_persist_db_tokens()`, which calls `save_atlassian_tokens()`. The `.env` file write is removed entirely.

**FR-2.5** Token writes are always atomic (single SQLite `INSERT OR REPLACE` transaction). There is no partial-write window. This closes the rotating-token lockout race (see Problem §2).

**FR-2.6** The SQLite database file is already excluded from git (`.gitignore` has `*.db` or the specific filename). Verify; add if missing. Tokens never appear in `.env` post-migration.

**FR-2.7** **Migration** — on app startup, if the `oauth_tokens` table has no `atlassian` row and `ATLASSIAN_OAUTH_TOKEN` is set in the environment, auto-import the env vars into the table and log `"Migrated Atlassian tokens from .env to SQLite — you can remove them from .env"`. No user action required.

---

### Idea 3 — Proactive Pre-Refresh

**FR-3.1** `_refresh_token()` in `confluence.py` stores `expires_at = int(time.time()) + expires_in` when writing tokens after a successful Atlassian exchange. `expires_in` comes directly from the Atlassian response body.

**FR-3.2** `_get_token()` checks `expires_at` before returning the access token. If `expires_at` is set and `time.time() + 60 >= expires_at` (i.e., expires within 60 seconds), it calls `_refresh_token()` proactively under `_token_lock` before returning the fresh token.

**FR-3.3** The reactive 401 refresh path in `_oauth_get()` and `_oauth_post()` is kept as a safety net. The combined proactive + reactive approach covers both the common case (clock-driven expiry) and edge cases (clock skew, Atlassian shortening the window unexpectedly).

---

## Non-Functional Requirements

**NFR-1** Token storage is atomic — `INSERT OR REPLACE` in a single SQLite transaction. No intermediate state is observable.

**NFR-2** No credentials appear in `.env` post-migration except `CLIENT_ID` and `CLIENT_SECRET` (needed to bootstrap before the first auth). After first auth, `CLIENT_ID`/`CLIENT_SECRET` are stored in `oauth_tokens.client_id`/`client_secret` so they survive env resets.

**NFR-3** `SESSION_SECRET` is documented in `.env.example` with a comment: `# Generate with: python -c "import secrets; print(secrets.token_hex(32))"`.

**NFR-4** The `GET /auth/status` endpoint does not require the `X-Api-Key` header — it is intentionally public so the frontend can call it before the user has entered a key.

---

## User-Facing Flows

### First-Time Setup

1. Dev adds `SESSION_SECRET` to `.env` (documented in `.env.example`).
2. Dev updates the Atlassian developer console callback URL to `http://localhost:8000/auth/callback` (one-time).
3. Dev starts the server: `uvicorn main:app`.
4. Opens the app in a browser — "Connect Confluence" button is visible in the header.
5. Clicks the button → browser redirects to Atlassian consent screen.
6. Dev approves access → Atlassian redirects to `/auth/callback` → tokens written to SQLite → browser redirected to `/`.
7. Header shows "Confluence ✓". Generation jobs proceed without any manual token steps.

### Token Expiry (Normal Operation)

With proactive pre-refresh, this is invisible to the user. Before serving a token within 60 seconds of its expiry, `_get_token()` silently refreshes and returns the new token. The generation job sees no interruption.

### Full Re-Auth (Tokens Revoked or DB Reset)

1. `GET /auth/status` returns `{"confluence_connected": false}`.
2. Header shows "Connect Confluence" button again.
3. User clicks, completes the OAuth flow, continues working.

---

## Key Files to Change

| File | Change |
|------|--------|
| `main.py` | Add `SessionMiddleware`; add `GET /auth/confluence`, `GET /auth/callback`, `GET /auth/status` |
| `tools/confluence.py` | Replace `_get_token()` + `_persist_env_tokens()` with DB-backed equivalents; add proactive pre-refresh to `_get_token()` |
| `db.py` | Add `oauth_tokens` table DDL; add `get_atlassian_tokens()` + `save_atlassian_tokens()` helpers |
| `static/index.html` | Add "Connect Confluence" / "Confluence ✓" header element |
| `static/app.js` | On page load, call `GET /auth/status` and toggle header element |
| `.env.example` | Document `SESSION_SECRET`; note callback URL change |
| `.gitignore` | Verify DB file is excluded |
| `scripts/atlassian_oauth.py` | Mark deprecated at top of file |

---

## Success Criteria

1. A developer who has never run the app can complete Confluence auth by clicking one button in the browser — no terminal commands, no `.env` edits beyond `SESSION_SECRET`, no server restart.
2. A rotating refresh token is never lost to a process crash (atomic SQLite write closes the race).
3. A generation job does not emit a Confluence 401 due to token expiry (proactive pre-refresh covers the 3600s window).
4. The `GET /auth/status` endpoint correctly reflects connection state after auth and after token loss.

---

## Open Questions for Planning

- Does the existing Atlassian developer console app use `client_secret_post` or `client_secret_basic` for token exchange? (The existing script uses `client_secret_post` — confirm this matches the app type.)
- PKCE requirement: Atlassian requires PKCE for public clients. Confirm whether the registered app is public or confidential (confidential apps may use `client_secret` alone without PKCE). If PKCE is required, the code verifier flow in FR-1.1/FR-1.2 is mandatory.
- `SESSION_SECRET` fallback behavior: if the server restarts while a user is mid-OAuth-flow (between `/auth/confluence` redirect and `/auth/callback`), the session is lost. Acceptable for a single-tenant dev tool — document this as expected.
