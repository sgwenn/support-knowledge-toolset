"""
OAuth route tests for soce-doc-agent.

Covers:
  - GET /auth/status  (connected / disconnected / expired / None expires_at)
  - GET /auth/callback (error param, state mismatch)
  - startup migration logic in _start_cleanup_task
"""

import time
from unittest.mock import patch, AsyncMock

import pytest


# ---------------------------------------------------------------------------
# /auth/status
# ---------------------------------------------------------------------------

class TestAuthStatus:
    async def test_no_tokens_returns_disconnected(self, client):
        """When no tokens in DB, returns connected=False."""
        with patch("main.get_atlassian_tokens", return_value=None):
            r = await client.get("/auth/status")
        assert r.status_code == 200
        assert r.json()["confluence_connected"] is False

    async def test_valid_token_returns_connected(self, client):
        """Token with future expiry returns connected=True."""
        row = {"expires_at": int(time.time()) + 3600, "access_token": "tok"}
        with patch("main.get_atlassian_tokens", return_value=row):
            r = await client.get("/auth/status")
        assert r.status_code == 200
        assert r.json()["confluence_connected"] is True

    async def test_expired_token_returns_disconnected(self, client):
        """Token past expiry returns connected=False."""
        row = {"expires_at": int(time.time()) - 100, "access_token": "tok"}
        with patch("main.get_atlassian_tokens", return_value=row):
            r = await client.get("/auth/status")
        assert r.status_code == 200
        assert r.json()["confluence_connected"] is False

    async def test_none_expires_at_returns_disconnected(self, client):
        """Migrated tokens with expires_at=None must NOT show as connected (finding #8)."""
        row = {"expires_at": None, "access_token": "migrated-tok"}
        with patch("main.get_atlassian_tokens", return_value=row):
            r = await client.get("/auth/status")
        assert r.status_code == 200
        assert r.json()["confluence_connected"] is False


# ---------------------------------------------------------------------------
# /auth/callback
# ---------------------------------------------------------------------------

class TestAuthCallback:
    async def test_error_param_redirects_to_access_denied(self, client):
        """If Atlassian returns error=access_denied, redirect contains auth_error=access_denied."""
        r = await client.get(
            "/auth/callback",
            params={"error": "access_denied"},
            follow_redirects=False,
        )
        assert r.status_code in (302, 307)
        assert "auth_error=access_denied" in r.headers["location"]

    async def test_state_mismatch_redirects(self, client):
        """If state doesn't match session, redirect contains auth_error=state_mismatch."""
        # No session cookie is set, so oauth_state will be None — guaranteed mismatch.
        r = await client.get(
            "/auth/callback",
            params={"code": "abc123", "state": "wrong-state"},
            follow_redirects=False,
        )
        assert r.status_code in (302, 307)
        assert "auth_error=state_mismatch" in r.headers["location"]

    async def test_no_code_redirects_to_exchange_failed(self, client):
        """If callback arrives with neither code nor error, redirect with exchange_failed."""
        # Inject a matching state so we pass the state check, then hit the missing-code branch.
        with patch("main.secrets.token_urlsafe", return_value="fixed-state"):
            # Start the flow to write the state into the session cookie.
            with patch.dict("os.environ", {"ATLASSIAN_CLIENT_ID": "test-client-id"}):
                init_r = await client.get("/auth/confluence", follow_redirects=False)
            session_cookie = init_r.cookies.get("session")

        r = await client.get(
            "/auth/callback",
            params={"state": "fixed-state"},  # no code, no error
            follow_redirects=False,
            cookies={"session": session_cookie} if session_cookie else {},
        )
        assert r.status_code in (302, 307)
        assert "auth_error=" in r.headers["location"]

    async def test_missing_client_credentials_redirects(self, client):
        """With a code but no ATLASSIAN_CLIENT_ID/SECRET configured, redirect with exchange_failed."""
        with patch("main.secrets.token_urlsafe", return_value="fixed-state"):
            with patch.dict("os.environ", {"ATLASSIAN_CLIENT_ID": "test-client-id"}):
                init_r = await client.get("/auth/confluence", follow_redirects=False)
            session_cookie = init_r.cookies.get("session")

        # Unset credentials so the route hits the "not (client_id and client_secret)" branch.
        with patch.dict("os.environ", {"ATLASSIAN_CLIENT_ID": "", "ATLASSIAN_CLIENT_SECRET": ""}):
            r = await client.get(
                "/auth/callback",
                params={"code": "somecode", "state": "fixed-state"},
                follow_redirects=False,
                cookies={"session": session_cookie} if session_cookie else {},
            )
        assert r.status_code in (302, 307)
        assert "auth_error=exchange_failed" in r.headers["location"]

    # TODO (integration): test the empty-resources branch by mocking httpx.AsyncClient
    # to return an empty list from /oauth/token/accessible-resources. Requires full
    # session-state injection and httpx transport patching — left as a future test.


# ---------------------------------------------------------------------------
# Startup migration (_start_cleanup_task)
# ---------------------------------------------------------------------------

class TestStartupMigration:
    async def test_migration_saves_tokens_from_env(self):
        """When ATLASSIAN_OAUTH_TOKEN set and no DB row, tokens are migrated to SQLite."""
        saved = {}

        def mock_get_tokens():
            return None  # no existing row

        def mock_save_tokens(access_token, refresh_token, expires_at, cloud_id):
            saved["access_token"] = access_token
            saved["cloud_id"] = cloud_id

        with patch("main.get_atlassian_tokens", mock_get_tokens), \
             patch("main.save_atlassian_tokens", mock_save_tokens), \
             patch("main.db_init"), \
             patch.dict("os.environ", {
                 "ATLASSIAN_OAUTH_TOKEN": "mytoken",
                 "ATLASSIAN_CLOUD_ID": "cloud123",
             }):
            from main import _start_cleanup_task
            await _start_cleanup_task()

        assert saved.get("access_token") == "mytoken"
        assert saved.get("cloud_id") == "cloud123"

    async def test_migration_skipped_when_row_exists(self):
        """When a DB row already exists, save_atlassian_tokens is NOT called."""
        called = []

        def mock_get_tokens():
            return {"access_token": "existing", "expires_at": 9999999999}

        def mock_save_tokens(*a, **kw):
            called.append(True)

        with patch("main.get_atlassian_tokens", mock_get_tokens), \
             patch("main.save_atlassian_tokens", mock_save_tokens), \
             patch("main.db_init"), \
             patch.dict("os.environ", {"ATLASSIAN_OAUTH_TOKEN": "newtoken"}):
            from main import _start_cleanup_task
            await _start_cleanup_task()

        assert not called, "save_atlassian_tokens should not be called when row already exists"

    async def test_migration_db_failure_is_swallowed(self):
        """DB failure during migration logs a warning but does NOT propagate (finding #14)."""
        def mock_get_tokens():
            return None

        def mock_save_tokens(*a, **kw):
            raise RuntimeError("DB unavailable")

        with patch("main.get_atlassian_tokens", mock_get_tokens), \
             patch("main.save_atlassian_tokens", mock_save_tokens), \
             patch("main.db_init"), \
             patch.dict("os.environ", {"ATLASSIAN_OAUTH_TOKEN": "mytoken"}):
            from main import _start_cleanup_task
            # Must not raise — exception is caught and logged as a warning.
            await _start_cleanup_task()

    async def test_migration_not_triggered_without_env_token(self):
        """When ATLASSIAN_OAUTH_TOKEN is absent, save_atlassian_tokens is not called."""
        called = []

        def mock_get_tokens():
            return None

        def mock_save_tokens(*a, **kw):
            called.append(True)

        env = {k: v for k, v in __import__("os").environ.items() if k != "ATLASSIAN_OAUTH_TOKEN"}
        with patch("main.get_atlassian_tokens", mock_get_tokens), \
             patch("main.save_atlassian_tokens", mock_save_tokens), \
             patch("main.db_init"), \
             patch("os.environ", env):
            from main import _start_cleanup_task
            await _start_cleanup_task()

        assert not called
