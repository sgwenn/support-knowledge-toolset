import logging
import os
import threading
import time

import httpx

from db import get_atlassian_tokens, save_atlassian_tokens

logger = logging.getLogger(__name__)

# Maps lowercase Snowflake PRIMARY_PRODUCT_COMPONENT values to canonical TS space page titles.
# Keys are the actual _ppc values from DIM_ZENDESK_TICKET (lowercased).
COMPONENT_ALIASES: dict[str, str] = {
    # High-volume PPCs
    "agent_ppc": "Agent",
    "apm_ppc": "APM",
    "cloud_integrations_ppc": "Cloud Integrations",
    "containers_orchestrators_kubernetes_ppc": "Containers",
    "dbm_ppc": "Database Monitoring - DBM",
    "logs_ppc": "Logs",
    "metrics_ppc": "Metrics",
    "monitors_ppc": "Monitors (Alerting Platform)",
    "new_and_misc_components_ppc": "New & Misc",
    "rum_ppc": "RUM",
    "security_ppc": "Cloud Security Products",
    "serverless_ppc": "Serverless",
    "service_mgmt_ppc": "Service Management",
    "synthetics_ppc": "Synthetics",
    "synthetics_rum_ppc": "RUM",
    "universal_service_monitoring_ppc": "Universal Service Monitoring",
    "web_platform_ppc": "Web Platform",
    # Lower-volume / newer PPCs
    "coscreen_ppc": "New & Misc",
    "post_sales_customer_success_ppc": "New & Misc",
    "support_ops_ppc": "New & Misc",
    "pre_sales_sales_ops_ppc": "New & Misc",
}


def resolve_component_to_ts_page(component: str) -> str:
    """Return the best TS page title for this Snowflake component value."""
    return COMPONENT_ALIASES.get(component.lower().strip(), component)


_token_lock = threading.Lock()
_space_id_lock = threading.Lock()
_ts_space_id: str | None = None
_persist_failed = False  # suppresses proactive refresh when DB writes are failing


class ConfluenceNotAuthenticatedError(Exception):
    """Raised when no Atlassian OAuth tokens are available (DB and env both absent)."""


def _persist_db_tokens(
    access_token: str,
    refresh_token: str | None,
    expires_at: int | None,
    cloud_id: str | None,
) -> None:
    """Write tokens atomically to the oauth_tokens SQLite table."""
    global _persist_failed
    try:
        save_atlassian_tokens(access_token, refresh_token, expires_at, cloud_id)
        _persist_failed = False
    except Exception as e:
        logger.warning(
            "Failed to persist Atlassian tokens to DB — proactive refresh suppressed until next successful save: %s",
            e,
        )
        _persist_failed = True


def _get_cloud_id() -> str:
    """Return the Atlassian cloud_id, preferring the DB row over the env var."""
    row = get_atlassian_tokens()
    if row and row.get("cloud_id"):
        return row["cloud_id"]
    cloud_id = os.environ.get("ATLASSIAN_CLOUD_ID", "")
    if not cloud_id:
        raise ConfluenceNotAuthenticatedError(
            "Confluence not connected — visit /auth/confluence to authenticate"
        )
    return cloud_id


def _refresh_token() -> str:
    """Exchange the refresh token for a new access token. Must be called under _token_lock."""
    client_id = os.environ.get("ATLASSIAN_CLIENT_ID", "")
    client_secret = os.environ.get("ATLASSIAN_CLIENT_SECRET", "")
    row = get_atlassian_tokens()
    refresh_token = (row or {}).get("refresh_token") or os.environ.get("ATLASSIAN_REFRESH_TOKEN", "")
    cloud_id = (row or {}).get("cloud_id") or os.environ.get("ATLASSIAN_CLOUD_ID", "")
    if not (client_id and client_secret and refresh_token):
        raise RuntimeError("Atlassian OAuth credentials not configured for token refresh")

    r = httpx.post(
        "https://auth.atlassian.com/oauth/token",
        json={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
        },
        timeout=10.0,
    )
    if not r.is_success:
        try:
            detail = r.json().get("error", "unknown")
        except Exception:
            detail = "unparseable"
        logger.error("Token refresh failed %s: %s", r.status_code, detail)
    r.raise_for_status()
    tokens = r.json()
    new_access = tokens["access_token"]
    new_refresh = tokens.get("refresh_token") or refresh_token
    expires_in = tokens.get("expires_in")
    expires_at = int(time.time()) + expires_in if expires_in else None

    _persist_db_tokens(new_access, new_refresh, expires_at, cloud_id)
    return new_access


def _get_token_unlocked() -> str:
    """Return the access token, proactively refreshing if near expiry. Must be called under _token_lock."""
    row = get_atlassian_tokens()
    if row:
        expires_at = row.get("expires_at")
        if expires_at and time.time() + 60 >= expires_at:
            if _persist_failed:
                logger.warning("Skipping proactive token refresh — DB persist is unhealthy; using existing token")
            else:
                return _refresh_token()
        if row.get("access_token"):
            return row["access_token"]
    token = os.environ.get("ATLASSIAN_OAUTH_TOKEN", "")
    if not token:
        raise ConfluenceNotAuthenticatedError(
            "Confluence not connected — visit /auth/confluence to authenticate"
        )
    return token


def _get_token() -> str:
    """Return the access token, acquiring _token_lock. Use _get_token_unlocked() when already holding the lock."""
    with _token_lock:
        return _get_token_unlocked()


def _api_base() -> str:
    cloud_id = _get_cloud_id()
    return f"https://api.atlassian.com/ex/confluence/{cloud_id}/wiki"


def _oauth_get(path: str, params: dict) -> httpx.Response:
    """GET against the Confluence Cloud API with automatic token refresh on 401."""
    with _token_lock:
        base = _api_base()

        def _do(token: str) -> httpx.Response:
            return httpx.get(
                base + path,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                params=params,
                timeout=10.0,
            )

        token_used = _get_token_unlocked()
        r = _do(token_used)
        if r.status_code == 401:
            # Re-read in case another thread already refreshed while we waited for the lock
            current = _get_token_unlocked()
            r = _do(current if current != token_used else _refresh_token())
        r.raise_for_status()
    return r


def _oauth_post(path: str, body: dict) -> httpx.Response:
    """POST against the Confluence Cloud API with automatic token refresh on 401."""
    with _token_lock:
        base = _api_base()

        def _do(token: str) -> httpx.Response:
            return httpx.post(
                base + path,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json", "Content-Type": "application/json"},
                json=body,
                timeout=30.0,
            )

        token_used = _get_token_unlocked()
        r = _do(token_used)
        if r.status_code == 401:
            current = _get_token_unlocked()
            r = _do(current if current != token_used else _refresh_token())
        r.raise_for_status()
    return r


def create_page(space_id: str, title: str, html_body: str, parent_page_id: str | None = None) -> dict:
    """Create a published Confluence page (used for KB Drafts folder creation). Returns the created page JSON."""
    body: dict = {
        "spaceId": space_id,
        "status": "current",
        "title": title,
        "body": {"representation": "storage", "value": html_body},
    }
    if parent_page_id:
        body["parentId"] = parent_page_id
    r = _oauth_post("/api/v2/pages", body)
    return r.json()


def create_draft_page_pat(space_id: str, title: str, html_body: str, parent_page_id: str | None = None, parent_type: str = "page") -> dict:
    """Create a Confluence draft page.

    Tries PAT auth first (so the page is owned by the real user, enabling resumedraft URLs).
    Falls back to the OAuth token if PAT auth is not configured or returns a 401/403/404.
    Pass parent_type="folder" when parent_page_id is a folder ID — the v2 API requires it.
    """
    body: dict = {
        "spaceId": space_id,
        "status": "draft",
        "title": title,
        "body": {"representation": "storage", "value": html_body},
    }
    if parent_page_id:
        body["parentId"] = parent_page_id
        body["parentType"] = parent_type

    email = os.environ.get("ATLASSIAN_USER_EMAIL", "")
    pat = os.environ.get("ATLASSIAN_API_TOKEN", "")
    if email and pat:
        try:
            r = httpx.post(
                "https://datadoghq.atlassian.net/wiki/api/v2/pages",
                auth=(email, pat),
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                json=body,
                timeout=30.0,
            )
            if r.status_code not in (401, 403, 404):
                r.raise_for_status()
                return r.json()
            logger.warning("PAT create failed (%s) — falling back to OAuth token", r.status_code)
        except httpx.HTTPStatusError:
            logger.warning("PAT create raised HTTPStatusError — falling back to OAuth token")

    # OAuth fallback — page will be owned by the OAuth app, not the user directly
    return _oauth_post("/api/v2/pages", body).json()


def find_child_folder_by_name(parent_id: str, name: str) -> str | None:
    """Return the folder ID of a direct child folder of parent_id matching name (case-insensitive)."""
    try:
        r = _oauth_get("/api/v2/folders", {"parentId": parent_id, "limit": 50})
        for folder in r.json().get("results", []):
            if folder.get("title", folder.get("name", "")).lower() == name.lower():
                return str(folder["id"])
    except Exception as e:
        logger.warning("Could not list child folders of %s: %s", parent_id, e)
    return None


def find_or_create_kb_drafts_folder(parent_page_id: str, space_id: str) -> str | None:
    """Return the ID of a 'KB Drafts' folder under parent_page_id, creating it if absent.

    Returns None if the Confluence folders API rejects the request (e.g. parentId is a page,
    not a folder — the API only supports folder-under-folder or folder-under-space).
    """
    existing = find_child_folder_by_name(parent_page_id, "KB Drafts")
    if existing:
        return existing
    try:
        r = _oauth_post("/api/v2/folders", {
            "spaceId": space_id,
            "title": "KB Drafts",
            "parentId": parent_page_id,
        })
        raw_id = r.json().get("id")
        folder_id = str(raw_id) if raw_id is not None else None
        if folder_id:
            logger.info("Created KB Drafts folder %s under page %s", folder_id, parent_page_id)
        return folder_id
    except Exception as e:
        logger.warning("Could not create KB Drafts folder under page %s (folders API may not support page parents): %s", parent_page_id, e)
    return None


def find_child_page_by_title(parent_page_id: str, title: str) -> str | None:
    """Return the page ID of a direct child of parent_page_id whose title matches (case-insensitive), or None."""
    try:
        r = _oauth_get(f"/api/v2/pages/{parent_page_id}/children", {"limit": 50})
        for child in r.json().get("results", []):
            if child.get("title", "").lower() == title.lower():
                return str(child["id"])
    except Exception as e:
        logger.warning("Could not list children of page %s: %s", parent_page_id, e)
    return None


def find_or_create_kb_drafts_page(ppc_page_id: str, space_id: str) -> str | None:
    """Return the ID of the 'KB Drafts' child page under ppc_page_id, creating it if absent."""
    existing = find_child_page_by_title(ppc_page_id, "KB Drafts")
    if existing:
        return existing
    try:
        created = create_page(space_id, "KB Drafts", "<p>AI-generated KB article drafts pending review.</p>", ppc_page_id)
        return str(created.get("id", "")) or None
    except Exception as e:
        logger.warning("Could not create KB Drafts page under %s: %s", ppc_page_id, e)
    return None


def get_ts_space_id() -> str | None:
    """Return the Confluence numeric space ID for the TS space, cached after first fetch."""
    global _ts_space_id
    with _space_id_lock:
        if _ts_space_id:
            return _ts_space_id
    try:
        _get_cloud_id()
    except ConfluenceNotAuthenticatedError:
        return None
    try:
        r = _oauth_get("/api/v2/spaces", {"keys": "TS", "limit": 1})
        results = r.json().get("results", [])
        if results:
            with _space_id_lock:
                _ts_space_id = str(results[0]["id"])
    except Exception as e:
        logger.warning("Failed to fetch TS space ID: %s", e)
    with _space_id_lock:
        return _ts_space_id


def find_parent_page_id(space_key: str, component: str) -> str | None:
    """Find a top-level page in space_key whose title matches component.

    Tries exact match first, then prefix match. Returns the page's content ID
    string, or None if no match is found.
    """
    if not component:
        return None
    try:
        _get_cloud_id()
    except ConfluenceNotAuthenticatedError:
        return None

    component_clean = component.strip('"').strip().replace('"', '\\"')
    safe_space_key = space_key.replace('"', '\\"')

    # Prefer pages under the "Product Areas" root (421167559) for TS space lookups
    ancestor_clause = 'ancestor = "421167559" AND ' if space_key == "TS" else ""

    for cql in [
        f'{ancestor_clause}space = "{safe_space_key}" AND type = page AND title = "{component_clean}"',
        f'{ancestor_clause}space = "{safe_space_key}" AND type = page AND title ~ "{component_clean}"',
    ]:
        try:
            r = _oauth_get("/rest/api/search", {"cql": cql, "limit": 1})
            results = r.json().get("results", [])
            if results:
                return results[0].get("content", {}).get("id")
        except Exception as e:
            logger.warning("CQL search failed (cql=%r): %s", cql, e)
    return None


def search_confluence(query: str, space_key: str = "TS") -> list:
    try:
        _get_cloud_id()
    except ConfluenceNotAuthenticatedError:
        return [{"error": "Atlassian OAuth credentials not configured"}]

    safe_query = query.replace('"', '\\"')
    safe_space_key = space_key.replace('"', '\\"')
    r = _oauth_get(
        "/rest/api/search",
        {
            "cql": f'space = "{safe_space_key}" AND type = page AND text ~ "{safe_query}"',
            "limit": 5,
            "excerpt": "highlight",
            "expand": "space",
        },
    )
    return [
        {
            "title": hit["title"],
            "url": f"https://datadoghq.atlassian.net/wiki{hit['url']}",
            "excerpt": hit.get("excerpt", ""),
        }
        for hit in r.json().get("results", [])
    ]
