import asyncio
import httpx
import json
import logging
import os
import re

import anthropic
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

from prompts import SYSTEM_PROMPT
from tools.snowflake import fetch_ticket, find_similar_tickets
from tools.algolia import search_docs
from tools.confluence import search_confluence, find_parent_page_id, get_ts_space_id, create_draft_page_pat, resolve_component_to_ts_page, find_or_create_kb_drafts_folder, find_or_create_kb_drafts_page

load_dotenv()

_client = anthropic.Anthropic(
    api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
    timeout=anthropic.Timeout(180.0, connect=5.0),
)

MODEL = "claude-sonnet-4-6"

CUSTOM_TOOL_DEFINITIONS = [
    {
        "name": "fetch_ticket_from_snowflake",
        "description": "Fetch a Zendesk ticket and its full conversation from Snowflake by ticket ID. Returns ticket metadata including IS_ESCALATED_TO_JIRA_ESCALATION, JIRA_ISSUE_KEY, and FULL_CONVERSATION.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "integer", "description": "The Zendesk ticket ID"}
            },
            "required": ["ticket_id"],
        },
    },
    {
        "name": "find_similar_tickets",
        "description": "Find the top 5 most similar past tickets using vector cosine similarity on pre-computed embeddings. Use the results as background context — do not cite ticket IDs in the article.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "integer", "description": "The Zendesk ticket ID to find similar tickets for"}
            },
            "required": ["ticket_id"],
        },
    },
    {
        "name": "search_docs",
        "description": "Search docs.datadoghq.com via Algolia. Pass up to 3 targeted queries based on the product area, error messages, and resolution. Returns titles, URLs, and key excerpts.",
        "input_schema": {
            "type": "object",
            "properties": {
                "queries": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 3,
                    "description": "Search queries based on the ticket's product area, errors, and resolution",
                }
            },
            "required": ["queries"],
        },
    },
    {
        "name": "search_confluence",
        "description": "Search a Confluence space for existing KB articles. Defaults to the TS (Technical Solutions) space. Pass space_key to narrow to a product-specific space if you know it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search terms based on the product area, error, or resolution"},
                "space_key": {"type": "string", "description": "Confluence space key to search (default: TS). Use the space for the ticket's product area if known."},
            },
            "required": ["query"],
        },
    },
]

_CUSTOM_TOOL_NAMES = {t["name"] for t in CUSTOM_TOOL_DEFINITIONS}


def _dispatch_tool(name: str, inputs: dict, prefetch_cache: dict = None):
    if name == "fetch_ticket_from_snowflake":
        tid = inputs["ticket_id"]
        if prefetch_cache is not None and tid in prefetch_cache:
            logger.debug("prefetch_cache hit for ticket %s — skipping Snowflake", tid)
            return prefetch_cache[tid]
        return fetch_ticket(tid)
    if name == "find_similar_tickets":
        return find_similar_tickets(inputs["ticket_id"])
    if name == "search_docs":
        return search_docs(inputs["queries"])
    if name == "search_confluence":
        return search_confluence(inputs["query"], inputs.get("space_key", "TS"))
    return {"error": f"Unknown tool: {name}"}


_ADF_BLOCK_TYPES = frozenset({
    "doc", "bulletList", "orderedList", "listItem",
    "blockquote", "codeBlock", "table", "tableRow",
    "tableCell", "panel", "expand",
})


def _extract_adf_text(node) -> str:
    """Recursively extract plain text from an Atlassian Document Format node."""
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "text":
        return node.get("text", "")
    parts = [_extract_adf_text(child) for child in (node.get("content") or [])]
    sep = "\n" if node.get("type") in _ADF_BLOCK_TYPES else ""
    return sep.join(p for p in parts if p)


async def _jira_preflight(queue: asyncio.Queue, ticket_id: int, jira_key: str) -> str:
    """Fetch the linked Jira issue before the agent loop and return a JIRA CONTEXT block.

    Emits SSE tool_call / tool_result events. Returns empty string on any failure.
    """
    await queue.put({"type": "tool_call", "name": "jira_preflight", "input_summary": f"{jira_key} (ZD #{ticket_id})"})
    try:
        from tools.confluence import _get_cloud_id, _get_token, ConfluenceNotAuthenticatedError
        try:
            cloud_id = _get_cloud_id()
            oauth_token = _get_token()
        except ConfluenceNotAuthenticatedError as e:
            raise ValueError(f"Atlassian OAuth not connected: {e}") from e

        base = f"https://api.atlassian.com/ex/jira/{cloud_id}"
        headers = {"Authorization": f"Bearer {oauth_token}", "Accept": "application/json"}

        _COMMENT_BODY_LIMIT = 300   # chars per comment
        _TOTAL_COMMENTS_LIMIT = 3000  # chars across all comments

        def _xml_escape(s: str) -> str:
            return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(15.0, connect=3.0)) as http:
            r = await http.get(
                f"{base}/rest/api/3/issue/{jira_key}",
                params={"fields": "summary,description,status,comment"},
            )
            r.raise_for_status()
            issue = r.json()
            key = issue["key"]
            fields = issue["fields"]
            summary = _xml_escape(fields.get("summary", ""))
            status = _xml_escape((fields.get("status") or {}).get("name", ""))
            description = _xml_escape(_extract_adf_text(fields.get("description") or {})[:800])

            # Fetch full comment history — the search API returns up to 10; fetch all if there are more.
            comment_block = fields.get("comment") or {}
            comments = comment_block.get("comments", [])
            comment_total = comment_block.get("total", len(comments))
            if comment_total > len(comments):
                cr = await http.get(
                    f"{base}/rest/api/3/issue/{key}/comment",
                    params={"maxResults": 100, "orderBy": "created"},
                )
                if cr.is_success:
                    comments = cr.json().get("comments", comments)

            comment_lines = []
            total_chars = 0
            for c in comments:
                author = (c.get("author") or {}).get("displayName", "Unknown")
                body_text = _extract_adf_text(c.get("body") or {})[:_COMMENT_BODY_LIMIT]
                if not body_text.strip():
                    continue
                entry = f"[{author}]: {_xml_escape(body_text)}"
                if total_chars + len(entry) > _TOTAL_COMMENTS_LIMIT:
                    comment_lines.append("[... later comments truncated for length]")
                    break
                comment_lines.append(entry)
                total_chars += len(entry)

            lines = [
                "<jira_context>",
                f"Issue: {key} — {summary}",
                f"Status: {status}",
                f"Description: {description}",
            ]
            if comment_lines:
                lines.append(f"Comment history ({len(comments)} comments):")
                lines.extend(comment_lines)
            lines.append("</jira_context>")

        await queue.put({"type": "tool_result", "name": "jira_preflight", "summary": f"Jira context loaded ({len(comment_lines)} comments)"})
        return "\n".join(lines)

    except Exception:
        logger.warning("Jira pre-flight failed for ticket %s", ticket_id, exc_info=True)
        await queue.put({"type": "tool_result", "name": "jira_preflight", "summary": "Jira lookup failed (skipped)"})
        return ""


def _build_initial_message(mode: str, ticket_id: int = None, raw_text: str = None) -> str:
    if mode == "ticket_id":
        return f"Generate a KB article for Zendesk ticket #{ticket_id}. Start by fetching it from Snowflake."
    return (
        "Generate a KB article from the following support ticket text. "
        "Do NOT call fetch_ticket_from_snowflake — work from the text below.\n\n"
        f"TICKET TEXT:\n{raw_text}"
    )


async def run_kb_agent(
    queue: asyncio.Queue,
    mode: str = "ticket_id",
    ticket_id: int = None,
    raw_text: str = None,
):
    loop = asyncio.get_running_loop()
    component_holder: list[str | None] = [None]
    prefetch_cache: dict = {}

    # Pre-flight: for ticket_id mode, deterministically fetch Jira context before the agent loop.
    # Store the result so _dispatch_tool can serve the first fetch_ticket_from_snowflake call from
    # cache, eliminating the duplicate Snowflake round-trip.
    jira_block = ""
    if mode == "ticket_id" and ticket_id:
        try:
            ticket_data = await loop.run_in_executor(None, lambda: fetch_ticket(ticket_id))
        except Exception:
            logger.warning("Pre-flight Snowflake fetch failed for ticket %s", ticket_id, exc_info=True)
            ticket_data = None
        if ticket_data:
            prefetch_cache[ticket_id] = ticket_data
        jira_key = ticket_data.get("jira_key") if ticket_data else None
        if ticket_data and jira_key:
            jira_block = await _jira_preflight(queue, ticket_id, jira_key)

    initial_msg = _build_initial_message(mode, ticket_id, raw_text)
    if jira_block:
        initial_msg = initial_msg + "\n\n" + jira_block
    messages = [{"role": "user", "content": initial_msg}]

    try:
        while True:
            response = await loop.run_in_executor(
                None,
                lambda: _client.beta.messages.create(
                    model=MODEL,
                    max_tokens=8192,
                    system=SYSTEM_PROMPT,
                    tools=CUSTOM_TOOL_DEFINITIONS,
                    messages=messages,
                ),
            )

            for block in response.content:
                if block.type == "text" and block.text.strip():
                    await queue.put({"type": "thinking", "text": block.text})

            if response.stop_reason not in ("end_turn", "tool_use"):
                if response.stop_reason == "max_tokens":
                    await queue.put({"type": "error", "message": "Article generation hit the token limit and was truncated. The ticket may be too long or complex."})
                else:
                    await queue.put({"type": "error", "message": f"Unexpected stop_reason: {response.stop_reason}"})
                break

            if response.stop_reason == "end_turn":
                text_blocks = [b.text for b in response.content if b.type == "text"]
                final_text = "\n".join(text_blocks)
                title, existing_url, draft_html = _parse_draft(final_text)
                if not draft_html:
                    await queue.put({"type": "error", "message": "Agent finished but produced no draft (DRAFT_HTML marker missing)"})
                    break
                await queue.put({
                    "type": "done",
                    "kind": "kb",
                    "result": {
                        "draft_title": title,
                        "existing_url": existing_url,
                        "draft_html": draft_html,
                        "component": component_holder[0],
                    },
                })
                break

            if response.stop_reason == "tool_use":
                # Dispatch all custom tool calls concurrently; skip MCP tools (handled by API)
                async def _run_tool(block):
                    if block.name not in _CUSTOM_TOOL_NAMES:
                        return {"type": "tool_result", "tool_use_id": block.id, "content": ""}
                    await queue.put({
                        "type": "tool_call",
                        "name": block.name,
                        "input_summary": _summarize_input(block.name, block.input),
                    })
                    result = await loop.run_in_executor(None, lambda b=block: _dispatch_tool(b.name, b.input, prefetch_cache))
                    if block.name == "fetch_ticket_from_snowflake" and isinstance(result, dict):
                        component_holder[0] = result.get("primary_product_component")
                    await queue.put({
                        "type": "tool_result",
                        "name": block.name,
                        "summary": _summarize_result(block.name, result),
                    })
                    return {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result, default=str),
                    }

                tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
                tool_results = await asyncio.gather(*[_run_tool(b) for b in tool_use_blocks])
                messages.append({"role": "assistant", "content": response.content})
                messages.append({"role": "user", "content": list(tool_results)})

    except Exception as e:
        await queue.put({"type": "error", "message": str(e)})


def _parse_draft(text: str) -> tuple:
    title = ""
    existing_url = ""
    draft_html = ""
    if "DRAFT_TITLE:" in text:
        title = text.split("DRAFT_TITLE:", 1)[1].split("\n")[0].strip()
    if "EXISTING_URL:" in text:
        raw = text.split("EXISTING_URL:", 1)[1].split("\n")[0].strip()
        existing_url = "" if raw == "NONE" else raw
    if "DRAFT_HTML:" in text:
        draft_html = text.split("DRAFT_HTML:", 1)[1].strip()
        # Strip trailing markdown code fence the model sometimes emits after the HTML
        draft_html = re.sub(r'\n```\s*$', '', draft_html).rstrip()
    return title, existing_url, draft_html


def _strip_dangerous_html(html: str) -> str:
    html = re.sub(r'<script\b[^>]*>.*?</script>', '', html, flags=re.IGNORECASE | re.DOTALL)
    html = re.sub(r'\s+on\w+\s*=\s*(?:"[^"]*"|\'[^\']*\')', '', html, flags=re.IGNORECASE)
    html = re.sub(r'(?i)(href|src)\s*=\s*["\']javascript:[^"\']*["\']', '', html, flags=re.IGNORECASE)
    return html


async def run_publish_agent(
    queue: asyncio.Queue,
    title: str,
    draft_html: str,
    existing_url: str = "",
    requester_email: str = "",
    component: str = "",
):
    draft_html = _strip_dangerous_html(draft_html)

    base = "https://datadoghq.atlassian.net"
    token = os.environ.get("ATLASSIAN_API_TOKEN", "")
    auth_email = os.environ.get("ATLASSIAN_USER_EMAIL", "")
    if not (token and auth_email):
        logger.debug("ATLASSIAN_API_TOKEN/EMAIL not set — PAT auth unavailable, OAuth path expected")
    auth = (auth_email, token)
    headers = {"Accept": "application/json", "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(auth=auth, headers=headers, timeout=30) as http:
            page_url = ""

            if existing_url:
                await queue.put({"type": "tool_call", "name": "Fetching existing page", "input_summary": existing_url})
                m = re.search(r"/pages/(\d+)", existing_url)
                if not m:
                    await queue.put({"type": "error", "message": f"Could not parse page ID from: {existing_url}"})
                    return
                page_id = m.group(1)
                r = await http.get(f"{base}/wiki/api/v2/pages/{page_id}", params={"body-format": "storage"})
                r.raise_for_status()
                page = r.json()
                existing_body = page.get("body", {}).get("storage", {}).get("value", "")
                version = page["version"]["number"]
                await queue.put({"type": "tool_result", "name": "Fetching existing page", "summary": page.get("title", page_id)})

                await queue.put({"type": "tool_call", "name": "Updating page", "input_summary": page.get("title", page_id)})
                r = await http.put(
                    f"{base}/wiki/api/v2/pages/{page_id}",
                    json={
                        "id": page_id,
                        "status": "draft",
                        "title": page["title"],
                        "version": {"number": version + 1},
                        "body": {"representation": "storage", "value": _strip_dangerous_html(existing_body) + "\n<hr/>\n" + draft_html},
                    },
                )
                r.raise_for_status()
                updated = r.json()
                page_url = base + updated.get("_links", {}).get("webui", f"/wiki/pages/{page_id}")
                await queue.put({"type": "tool_result", "name": "Updating page", "summary": "Page updated"})

            else:
                # Resolve publish destination: component parent page in TS → KB Drafts in TS → personal space
                loop = asyncio.get_running_loop()
                ts_space_id = await loop.run_in_executor(None, get_ts_space_id)
                parent_page_id = None
                destination_label = "TS"

                if ts_space_id and component:
                    parent_page_id = await loop.run_in_executor(None, lambda: find_parent_page_id("TS", component))
                    if parent_page_id:
                        destination_label = component
                    else:
                        # Try alias (e.g. "Application Performance Monitoring" → "APM")
                        resolved = resolve_component_to_ts_page(component)
                        if resolved != component:
                            logger.info("PPC %r not found in TS; retrying as %r", component, resolved)
                            parent_page_id = await loop.run_in_executor(None, lambda: find_parent_page_id("TS", resolved))
                            if parent_page_id:
                                destination_label = resolved

                if ts_space_id and not parent_page_id:
                    # Fall back to root-level KB Drafts folder in TS space
                    if component:
                        logger.warning("No TS page found for component %r — draft will land in root KB Drafts", component)
                    parent_page_id = await loop.run_in_executor(None, lambda: find_parent_page_id("TS", "KB Drafts"))
                    destination_label = "KB Drafts"

                if ts_space_id:
                    # Route: TS > [PPC page] > KB Drafts (folder) > article (draft)
                    #        TS > [PPC page] > KB Drafts (page)   > article (draft)  [folder API fallback]
                    #        TS > KB Drafts (folder/page) > article (draft)           [no PPC page fallback]
                    kb_drafts_folder_id = None
                    kb_drafts_page_id = None
                    if parent_page_id:
                        kb_drafts_folder_id = await loop.run_in_executor(
                            None, lambda: find_or_create_kb_drafts_folder(parent_page_id, ts_space_id)
                        )
                        if not kb_drafts_folder_id:
                            # Confluence folders API doesn't support page parents — fall back to a child page
                            logger.info("Folder creation failed; falling back to KB Drafts child page under %s", parent_page_id)
                            kb_drafts_page_id = await loop.run_in_executor(
                                None, lambda: find_or_create_kb_drafts_page(parent_page_id, ts_space_id)
                            )

                    if not kb_drafts_folder_id and not kb_drafts_page_id:
                        logger.warning("KB Drafts container unavailable — placing article directly under parent")

                    final_parent_id = kb_drafts_folder_id or kb_drafts_page_id or parent_page_id
                    final_parent_type = "folder" if kb_drafts_folder_id else "page"
                    folder_label = f"{destination_label} > KB Drafts" if (kb_drafts_folder_id or kb_drafts_page_id) else destination_label

                    await queue.put({"type": "tool_call", "name": "Creating draft page", "input_summary": f"{title} → {folder_label}"})
                    created = await loop.run_in_executor(
                        None, lambda: create_draft_page_pat(ts_space_id, title, draft_html, final_parent_id, final_parent_type)
                    )
                    webui = created.get("_links", {}).get("webui", f"/wiki/pages/{created.get('id', 'unknown')}")
                    if not webui.startswith("/wiki"):
                        webui = "/wiki" + webui
                    page_url = "https://datadoghq.atlassian.net" + webui
                    await queue.put({"type": "tool_result", "name": "Creating draft page", "summary": f"Draft created in {folder_label}"})

                else:
                    # Final fallback: personal space
                    lookup_email = requester_email or auth_email
                    await queue.put({"type": "tool_call", "name": "Looking up user", "input_summary": lookup_email})
                    r = await http.get(f"{base}/rest/api/3/user/search", params={"query": lookup_email, "maxResults": 1})
                    r.raise_for_status()
                    users = r.json()
                    if not users:
                        await queue.put({"type": "error", "message": f"No Atlassian user found for {lookup_email}"})
                        return
                    account_id = users[0]["accountId"]
                    ps_key = f"~{account_id.replace(':', '').replace('-', '')}"
                    await queue.put({"type": "tool_result", "name": "Looking up user", "summary": f"account {account_id[:12]}…"})

                    await queue.put({"type": "tool_call", "name": "Finding personal space", "input_summary": ps_key})
                    r = await http.get(f"{base}/wiki/api/v2/spaces", params={"keys": ps_key, "limit": 1})
                    r.raise_for_status()
                    spaces = r.json().get("results", [])
                    if not spaces:
                        await queue.put({"type": "error", "message": f"Personal Confluence space not found for {lookup_email}"})
                        return
                    space_id = spaces[0]["id"]
                    await queue.put({"type": "tool_result", "name": "Finding personal space", "summary": spaces[0].get("name", ps_key)})

                    await queue.put({"type": "tool_call", "name": "Creating draft page", "input_summary": title})
                    r = await http.post(
                        f"{base}/wiki/api/v2/pages",
                        json={"spaceId": space_id, "status": "draft", "title": title, "body": {"representation": "storage", "value": draft_html}},
                    )
                    r.raise_for_status()
                    created = r.json()
                    webui = created.get("_links", {}).get("webui", f"/wiki/pages/{created.get('id', 'unknown')}")
                    if not webui.startswith("/wiki"):
                        webui = "/wiki" + webui
                    page_url = base + webui
                    await queue.put({"type": "tool_result", "name": "Creating draft page", "summary": "Draft created (personal space)"})

        await queue.put({"type": "done", "kind": "publish", "result": {"confluence_url": page_url, "title": title, "component": component}})

    except httpx.HTTPStatusError as e:
        try:
            body = e.response.text[:300]
        except Exception:
            body = ""
        logger.error("Confluence API error %s on %s %s: %s", e.response.status_code, e.request.method, e.request.url, body)
        await queue.put({"type": "error", "message": f"Confluence API error {e.response.status_code}"})
    except Exception as e:
        await queue.put({"type": "error", "message": f"Publish failed: {type(e).__name__}: {e}"})


def _summarize_input(tool_name: str, inputs: dict) -> str:
    if tool_name == "fetch_ticket_from_snowflake":
        return f"ticket #{inputs.get('ticket_id')}"
    if tool_name == "find_similar_tickets":
        return f"ticket #{inputs.get('ticket_id')}"
    if tool_name == "search_docs":
        queries = inputs.get("queries", [])
        return f"{queries[0][:60]}..." if queries else ""
    if tool_name == "search_confluence":
        space = inputs.get("space_key", "TS")
        return f"{inputs.get('query', '')[:60]} [{space}]"
    return str(inputs)[:80]


def _summarize_result(tool_name: str, result) -> str:
    if tool_name == "fetch_ticket_from_snowflake":
        if not result:
            return "Ticket not found"
        return f"{result.get('subject', '')[:80]} · {result.get('priority', '')} · {result.get('status', '')}"
    if tool_name == "find_similar_tickets":
        n = len(result) if isinstance(result, list) else 0
        return f"Found {n} similar ticket{'s' if n != 1 else ''}"
    if tool_name == "search_docs":
        n = len(result) if isinstance(result, list) else 0
        return f"Found {n} doc snippet{'s' if n != 1 else ''}"
    if tool_name == "search_confluence":
        n = len(result) if isinstance(result, list) else 0
        return f"Found {n} article{'s' if n != 1 else ''}"
    return str(result)[:100]
