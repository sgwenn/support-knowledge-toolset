import asyncio
import json
import logging
import os
import secrets
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.security import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from agent import run_kb_agent, run_publish_agent
from db import init as db_init, add_gap_records, add_coverage_record, get_coverage, get_atlassian_tokens, save_atlassian_tokens
from tools.snowflake import weekly_digest_candidates, batch_novelty_check

app = FastAPI(title="KB Agent")

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

_session_secret = os.environ.get("SESSION_SECRET", "")
if not _session_secret:
    raise RuntimeError(
        "SESSION_SECRET is not set. Generate one with: "
        "python -c \"import secrets; print(secrets.token_hex(32))\""
    )
_https_only = os.environ.get("APP_ENV", "development") == "production"
app.add_middleware(SessionMiddleware, secret_key=_session_secret, same_site="lax", https_only=_https_only, max_age=300)

_ATLASSIAN_AUTH_URL = "https://auth.atlassian.com/authorize"
_ATLASSIAN_TOKEN_URL = "https://auth.atlassian.com/oauth/token"
_ATLASSIAN_RESOURCES_URL = "https://api.atlassian.com/oauth/token/accessible-resources"
_ATLASSIAN_REDIRECT_URI = os.environ.get("ATLASSIAN_REDIRECT_URI", "http://localhost:8000/auth/callback")
_ATLASSIAN_SCOPES = (
    "read:page:confluence write:page:confluence "
    "read:folder:confluence write:folder:confluence "
    "read:space:confluence search:confluence "
    "read:jira-work offline_access"
)

# In-memory job store: job_id → asyncio.Queue
_jobs: dict[str, asyncio.Queue] = {}
_jobs_created: dict[str, float] = {}
_active_streams: set[str] = set()
_MAX_ACTIVE_JOBS = 10
_JOB_TTL_SECONDS = 300  # 5 minutes

_api_key_header = APIKeyHeader(name="X-Api-Key", auto_error=False)


async def require_api_key(key: str = Depends(_api_key_header)) -> None:
    expected = os.environ.get("KB_API_KEY")
    if expected and key != expected:
        raise HTTPException(status_code=403, detail="Invalid API key")


def _register_job() -> tuple[str, asyncio.Queue]:
    if len(_jobs) >= _MAX_ACTIVE_JOBS:
        raise HTTPException(429, f"Too many active jobs (max {_MAX_ACTIVE_JOBS})")
    job_id = str(uuid.uuid4())
    queue: asyncio.Queue = asyncio.Queue()
    _jobs[job_id] = queue
    _jobs_created[job_id] = time.monotonic()
    return job_id, queue


def _cleanup_job(job_id: str) -> None:
    _jobs.pop(job_id, None)
    _jobs_created.pop(job_id, None)
    _active_streams.discard(job_id)


@app.on_event("startup")
async def _start_cleanup_task() -> None:
    db_init()
    loop = asyncio.get_running_loop()
    try:
        row = await loop.run_in_executor(None, get_atlassian_tokens)
        if row is None and os.environ.get("ATLASSIAN_OAUTH_TOKEN"):
            await loop.run_in_executor(None, lambda: save_atlassian_tokens(
                access_token=os.environ["ATLASSIAN_OAUTH_TOKEN"],
                refresh_token=os.environ.get("ATLASSIAN_REFRESH_TOKEN"),
                expires_at=None,
                cloud_id=os.environ.get("ATLASSIAN_CLOUD_ID"),
            ))
            logger.info("Migrated Atlassian tokens from .env to SQLite — you can remove them from .env")
    except Exception as e:
        logger.warning("Startup token migration failed: %s", e)

    if os.environ.get("ATLASSIAN_API_TOKEN"):
        try:
            oauth_row = await loop.run_in_executor(None, get_atlassian_tokens)
            if oauth_row:
                logger.warning(
                    "ATLASSIAN_API_TOKEN is set alongside OAuth tokens — pages created via the PAT "
                    "path will be owned by the service account while OAuth-created pages are owned "
                    "by the authenticated user. Remove ATLASSIAN_API_TOKEN from .env to use OAuth exclusively."
                )
        except Exception:
            pass

    async def _cleanup_loop():
        while True:
            await asyncio.sleep(60)
            cutoff = time.monotonic() - _JOB_TTL_SECONDS
            stale = [jid for jid, t in list(_jobs_created.items()) if t < cutoff]
            for jid in stale:
                _cleanup_job(jid)
    asyncio.create_task(_cleanup_loop())


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    return (STATIC_DIR / "index.html").read_text()


@app.get("/auth/confluence")
async def auth_confluence(request: Request):
    client_id = os.environ.get("ATLASSIAN_CLIENT_ID", "")
    if not client_id:
        raise HTTPException(400, "ATLASSIAN_CLIENT_ID is not configured")
    state = secrets.token_urlsafe(16)
    request.session["oauth_state"] = state
    params = {
        "audience": "api.atlassian.com",
        "client_id": client_id,
        "scope": _ATLASSIAN_SCOPES,
        "redirect_uri": _ATLASSIAN_REDIRECT_URI,
        "response_type": "code",
        "prompt": "consent",
        "state": state,
    }
    url = _ATLASSIAN_AUTH_URL + "?" + urllib.parse.urlencode(params)
    return RedirectResponse(url)


@app.get("/auth/callback")
async def auth_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
):
    if error:
        return RedirectResponse("/?auth_error=access_denied")

    expected_state = request.session.pop("oauth_state", None)
    if not expected_state or state != expected_state:
        return RedirectResponse("/?auth_error=state_mismatch")

    if not code:
        return RedirectResponse("/?auth_error=exchange_failed")

    client_id = os.environ.get("ATLASSIAN_CLIENT_ID", "")
    client_secret = os.environ.get("ATLASSIAN_CLIENT_SECRET", "")
    if not (client_id and client_secret):
        return RedirectResponse("/?auth_error=exchange_failed")

    try:
        async with httpx.AsyncClient() as client:
            token_resp = await client.post(
                _ATLASSIAN_TOKEN_URL,
                json={
                    "grant_type": "authorization_code",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "code": code,
                    "redirect_uri": _ATLASSIAN_REDIRECT_URI,
                },
                timeout=10.0,
            )
            token_resp.raise_for_status()
            tokens = token_resp.json()

            access_token = tokens["access_token"]
            refresh_token = tokens.get("refresh_token")
            expires_in = tokens.get("expires_in")
            expires_at = int(time.time()) + expires_in if expires_in else None

            resources_resp = await client.get(
                _ATLASSIAN_RESOURCES_URL,
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
                timeout=10.0,
            )
            resources_resp.raise_for_status()
            resources = resources_resp.json()
    except Exception as e:
        logger.error("Atlassian OAuth callback failed: %s", e)
        return RedirectResponse("/?auth_error=exchange_failed")

    if not resources:
        return RedirectResponse("/?auth_error=no_resources")
    dd_resource = next(
        (r for r in resources if "datadoghq" in r.get("url", "")),
        resources[0],
    )
    cloud_id = dd_resource.get("id")

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        None, lambda: save_atlassian_tokens(access_token, refresh_token, expires_at, cloud_id)
    )
    return RedirectResponse("/")


@app.get("/auth/status")
async def auth_status(_: None = Depends(require_api_key)):
    loop = asyncio.get_running_loop()
    row = await loop.run_in_executor(None, get_atlassian_tokens)
    if row is None:
        return {"confluence_connected": False}
    expires_at = row.get("expires_at")
    connected = bool(expires_at and expires_at > int(time.time()))
    return {"confluence_connected": connected}


class RunRequest(BaseModel):
    mode: str = "ticket_id"          # "ticket_id" | "raw_text"
    ticket_id: Optional[int] = None
    raw_text: Optional[str] = Field(default=None, max_length=20_000)
    requester_email: Optional[str] = None


@app.post("/run")
async def run(req: RunRequest, _: None = Depends(require_api_key)):
    if req.mode not in ("ticket_id", "raw_text"):
        raise HTTPException(422, f"mode must be 'ticket_id' or 'raw_text'")
    if req.mode == "ticket_id" and not req.ticket_id:
        raise HTTPException(400, "ticket_id is required when mode is 'ticket_id'")
    if req.mode == "raw_text" and not req.raw_text:
        raise HTTPException(400, "raw_text is required when mode is 'raw_text'")

    job_id, queue = _register_job()

    asyncio.create_task(
        run_kb_agent(
            queue=queue,
            mode=req.mode,
            ticket_id=req.ticket_id,
            raw_text=req.raw_text,
        )
    )
    return {"job_id": job_id}


@app.get("/stream/{job_id}")
async def stream(job_id: str, request: Request):
    expected = os.environ.get("KB_API_KEY")
    if expected and request.query_params.get("key") != expected:
        raise HTTPException(status_code=403, detail="Invalid API key")
    if job_id not in _jobs:
        raise HTTPException(404, "Job not found")
    if job_id in _active_streams:
        raise HTTPException(409, "Stream already has an active consumer")

    queue = _jobs[job_id]
    loop = asyncio.get_event_loop()

    async def event_generator():
        _active_streams.add(job_id)
        heartbeat_interval = 15
        elapsed = 0
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=1.0)
                    yield f"data: {json.dumps(event)}\n\n"
                    if event.get("type") == "done" and event.get("kind") == "publish":
                        _result = event.get("result", {})
                        _comp = _result.get("component")
                        _url = _result.get("confluence_url")
                        if _comp and _url:
                            try:
                                await loop.run_in_executor(
                                    None, lambda c=_comp, u=_url, t=_result.get("title", ""): add_coverage_record(c, u, t)
                                )
                            except Exception as e:
                                logger.warning("coverage record persistence failed: %s", e)
                    if event.get("type") in ("done", "error"):
                        break
                    elapsed = 0
                except asyncio.TimeoutError:
                    elapsed += 1
                    if elapsed >= heartbeat_interval:
                        yield "data: {\"type\": \"heartbeat\"}\n\n"
                        elapsed = 0
        finally:
            _cleanup_job(job_id)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class PublishRequest(BaseModel):
    title: str
    draft_html: str
    existing_url: Optional[str] = None
    requester_email: Optional[str] = None
    component: Optional[str] = None


@app.post("/publish")
async def publish(req: PublishRequest, _: None = Depends(require_api_key)):
    allowed_domain = os.environ.get("ATLASSIAN_ALLOWED_EMAIL_DOMAIN", "@datadoghq.com")
    if req.requester_email and not req.requester_email.endswith(allowed_domain):
        raise HTTPException(400, f"requester_email must be a {allowed_domain} address")

    job_id, queue = _register_job()
    asyncio.create_task(
        run_publish_agent(
            queue=queue,
            title=req.title,
            draft_html=req.draft_html,
            existing_url=req.existing_url or "",
            requester_email=req.requester_email or "",
            component=req.component or "",
        )
    )
    return {"job_id": job_id}


@app.get("/digest")
async def digest(component: Optional[str] = None, _: None = Depends(require_api_key)):
    loop = asyncio.get_running_loop()

    candidates = await loop.run_in_executor(None, lambda: weekly_digest_candidates(component))
    if not candidates:
        return {"tickets": [], "patterns": "", "component": component}

    scored = [{**t, "_score": _score_ticket(t)} for t in candidates]
    scored.sort(key=lambda x: x["_score"], reverse=True)
    top10 = scored[:10]

    # Single batched novelty query instead of 10 parallel Snowflake connections
    tickets_with_ppc = [t for t in top10 if t.get("primary_product_component")]
    try:
        novelty_scores = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: batch_novelty_check(tickets_with_ppc)),
            timeout=25.0,
        )
    except Exception:
        novelty_scores = {}

    def _apply_novelty(ticket):
        if not ticket.get("primary_product_component"):
            return {**ticket, "_novelty_tag": None}
        sim = novelty_scores.get(ticket["id"], 0.5)
        adjustment, tag = 0, None
        if sim < 0.82:
            adjustment, tag = 2, "Novel issue"
        elif sim > 0.88:
            adjustment = -1
        return {**ticket, "_score": ticket["_score"] + adjustment, "_novelty_tag": tag}

    top10_with_novelty = [_apply_novelty(t) for t in top10]
    top10_with_novelty.sort(key=lambda x: (x["_score"], x.get("ticket_complexity") == "high"), reverse=True)
    top5 = top10_with_novelty[:5]

    records = [
        {
            "ticket_id": t["id"],
            "subject": t.get("subject"),
            "component": t["primary_product_component"],
            "score": t.get("_score", 0),
        }
        for t in top5 if t.get("primary_product_component")
    ]
    try:
        await loop.run_in_executor(None, lambda: add_gap_records(records))
    except Exception as e:
        logger.warning("gap record persistence failed: %s", e)

    return {
        "tickets": [_format_digest_ticket(t) for t in top5],
        "patterns": "",
        "component": component,
    }


@app.get("/coverage")
async def coverage(_: None = Depends(require_api_key)):
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, get_coverage)
    return data


def _score_ticket(t: dict) -> int:
    score = 0
    investigation = (t.get("investigation") or "").lower()
    solution = (t.get("suggested_solution_by_agents") or "").lower()

    non_obvious_keywords = ["hypothesis", "dead end", "ruled out", "source code", "trace", "backend", "root cause"]
    if any(kw in investigation for kw in non_obvious_keywords):
        score += 2
    solution_keywords = ["config", "parameter", "workaround", "flag", "version", "upgrade", "patch"]
    if any(kw in solution for kw in solution_keywords) and "check the docs" not in solution:
        score += 2
    if len(investigation) > 300:
        score += 2
    trivial_keywords = ["api key", "wrong key", "misconfiguration", "hadn't read", "simple"]
    if any(kw in investigation for kw in trivial_keywords):
        score -= 2

    if t.get("ticket_complexity") == "high":
        score += 2
    if t.get("ticket_impact") in ("high", "critical"):
        score += 1
    if t.get("is_escalated_to_jira_escalation") or t.get("is_escalated_to_engineering"):
        score += 1

    return score


def _conversation_snippet(full_conversation: str | None, max_chars: int = 400) -> str | None:
    if not full_conversation:
        return None
    # Strip leading speaker labels like "User:\n" or "Agent:\n" from the first turn
    text = full_conversation.strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "…"


def _format_digest_ticket(t: dict) -> dict:
    tags = []
    if t.get("_novelty_tag"):
        tags.append(t["_novelty_tag"])
    if t.get("is_escalated_to_jira_escalation"):
        tags.append("Jira escalation")
    if t.get("is_escalated_to_engineering"):
        tags.append("Eng escalation")
    if t.get("opex_description"):
        tags.append("OPEX flagged")
    if t.get("ticket_complexity") == "high":
        tags.append("High complexity")

    score = t.get("_score", 0)
    return {
        "id": t.get("id"),
        "subject": t.get("subject"),
        "component": t.get("primary_product_component"),
        "solved_timestamp": str(t.get("solved_timestamp", "")),
        "tags": tags,
        "value": "High value" if score >= 7 else "Medium value",
        "summary": t.get("summary"),
        "investigation": t.get("investigation") or _conversation_snippet(t.get("full_conversation")),
        "suggested_solution": t.get("suggested_solution_by_agents"),
        "score": score,
    }
