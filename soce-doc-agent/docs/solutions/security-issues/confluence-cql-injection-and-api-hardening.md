---
title: "CQL Injection, Stored XSS, and API Hardening in Confluence/Snowflake Integrations"
date: 2026-06-25
category: security-issues
module: "tools/confluence.py, tools/snowflake.py, agent.py"
problem_type: security-issues
component: "Confluence API, Snowflake"
symptoms:
  - Confluence search returns zero or wrong results when a component name contains a double-quote (e.g. APM "tracing")
  - Script tags stored in a Confluence page get re-appended verbatim on each agent update
  - High request latency under concurrent load due to Snowflake health-check holding a lock during a network round-trip
  - Snowflake-side statement handle exhaustion after many requests in a long session
  - KB article published to wrong Confluence location with no log explanation (silent space ID lookup failure)
  - DeprecationWarning for asyncio.get_event_loop() on Python 3.10+
  - KeyError crash when Confluence page-create returns an error body without an id field
root_cause: "LLM-controlled strings (query, space_key, component) interpolated raw into CQL without escaping double-quotes; existing Confluence page body not sanitized before re-storage; SELECT 1 health-check ran under _conn_lock; cursors opened without context managers at 6 call sites; _ts_space_id global read/written without a lock; bare except: pass blocks swallowed failures silently; asyncio.get_event_loop() deprecated in 3.10+; created['id'] accessed without .get() fallback"
resolution_type: code-fix
severity: P1
tags:
  - cql-injection
  - xss
  - snowflake
  - confluence
  - cursor-leak
  - thread-safety
  - asyncio
  - hardening
related_issues: []
---

# CQL Injection, Stored XSS, and API Hardening in Confluence/Snowflake Integrations

## Problem

A parallel code review of `soce-doc-agent` — a Python FastAPI + Anthropic agentic loop that fetches Zendesk tickets from Snowflake, searches Confluence for KB articles, and publishes AI-generated drafts — uncovered two P1 security vulnerabilities and five P2/P3 reliability defects. Left unaddressed, these issues ranged from CQL injection and stored XSS to Snowflake cursor leaks and thread-safety races that would destabilize the service under real concurrent load.

## Symptoms

**CQL injection (Fix 1):** Confluence search returns zero results or unexpected pages when a component or query string contains a double-quote character (e.g., a product name like `APM "tracing"`). In adversarial scenarios, a crafted component name could manipulate CQL `AND`/`OR` logic to search across unintended spaces.

**Stored XSS (Fix 2):** A Confluence page that previously contained a `<script>` tag or inline event handler (e.g., `<img onerror="...">`) would have that payload re-stored verbatim each time the agent appended a new draft.

**SELECT 1 under lock (Fix 3):** Under concurrent load, requests serialize at the Snowflake connection health check — one thread holds `_conn_lock` while executing a full network round-trip, blocking all others.

**Cursor leaks (Fix 4):** The service's memory footprint and Snowflake-side open statement handles grow monotonically. Under sustained load, Snowflake may begin rejecting new statement handles.

**Thread-unsafe cache (Fix 5):** Multiple concurrent requests on startup may each trigger a Confluence API call to resolve the TS space ID (TOCTOU race on `_ts_space_id`).

**Silent bare excepts (Fix 6):** When the Confluence space ID lookup fails, the service continues silently and publishes to an unexpected location with no log trace.

**Deprecated asyncio API (Fix 7):** On Python 3.12+, every call to the publish path emits `DeprecationWarning` for `asyncio.get_event_loop()`.

**KeyError on created['id'] (Fix 8):** If the Confluence page-create API returns an error body without an `id` field, the agent crashes with `KeyError` at URL construction.

## What Didn't Work

Two patterns were flagged during review but confirmed safe after inspection:

**Double-401 token refresh race:** `_token_lock` covers the entire attempt-and-refresh sequence as a single atomic unit. A thread that loses the lock race acquires it only after the winner has already stored the new token — no double-refresh.

**Lambda closure over `component`:** The lambda in `run_publish_agent` captures `component`, which is assigned once before the lambda is created and never mutated in a loop — no late-binding closure bug.

## Solution

### Fix 1: Escape user-controlled values before interpolating into CQL

**File:** `tools/confluence.py`, lines 128 and 148

CQL uses double-quoted string literals; a `"` inside a value terminates the literal and allows injection.

```python
# Before
component_clean = component.strip('"').strip()
for cql in [
    f'space = "{space_key}" AND type = page AND title = "{component_clean}"',
    f'space = "{space_key}" AND type = page AND title ~ "{component_clean}"',
]:
    ...
"cql": f'space = "{space_key}" AND type = page AND text ~ "{query}"',

# After
component_clean = component.strip('"').strip().replace('"', '\\"')
safe_space_key = space_key.replace('"', '\\"')
for cql in [
    f'space = "{safe_space_key}" AND type = page AND title = "{component_clean}"',
    f'space = "{safe_space_key}" AND type = page AND title ~ "{component_clean}"',
]:
    ...
safe_query = query.replace('"', '\\"')
safe_space_key = space_key.replace('"', '\\"')
"cql": f'space = "{safe_space_key}" AND type = page AND text ~ "{safe_query}"',
```

Note: `component.strip('"')` removed surrounding quotes from LLM output but did not protect against embedded quotes. The fix adds `.replace('"', '\\"')` after the strip.

---

### Fix 2: Sanitize existing Confluence body before re-storing

**File:** `agent.py`, line 359

```python
# Before
"body": {"representation": "storage", "value": existing_body + "\n<hr/>\n" + draft_html},

# After
"body": {"representation": "storage", "value": _strip_dangerous_html(existing_body) + "\n<hr/>\n" + draft_html},
```

`_strip_dangerous_html` was already applied to `draft_html` at line 325. It was never applied to `existing_body`, which comes from the Confluence API and may contain attacker-controlled content from a prior edit.

---

### Fix 3: Replace SELECT 1 health-check with is_closed()

**File:** `tools/snowflake.py`, lines 11–17

```python
# Before
with _conn_lock:
    if _conn is not None:
        try:
            _conn.cursor().execute("SELECT 1")
            return _conn
        except Exception:
            _conn = None

# After
with _conn_lock:
    if _conn is not None and not _conn.is_closed():
        return _conn
    _conn = None
```

`SnowflakeConnection.is_closed()` reads from an in-memory flag — no network I/O, no cursor, O(1) under lock.

---

### Fix 4: Use cursors as context managers

**File:** `tools/snowflake.py`, all 6 query functions

```python
# Before
conn = _get_conn()
cur = conn.cursor(snowflake.connector.DictCursor)
cur.execute(sql, (ticket_id,))
row = cur.fetchone()

# After
conn = _get_conn()
with conn.cursor(snowflake.connector.DictCursor) as cur:
    cur.execute(sql, (ticket_id,))
    row = cur.fetchone()
```

The `with` block guarantees `cur.close()` is called even if `execute` or `fetchone` raises. Snowflake cursors implement `__enter__`/`__exit__`.

---

### Fix 5: Guard the module-level cache with a threading.Lock

**File:** `tools/confluence.py`

```python
# Before
_ts_space_id: str | None = None

def get_ts_space_id():
    global _ts_space_id
    if _ts_space_id:
        return _ts_space_id
    ...
    if results:
        _ts_space_id = str(results[0]["id"])

# After
_space_id_lock = threading.Lock()
_ts_space_id: str | None = None

def get_ts_space_id():
    global _ts_space_id
    with _space_id_lock:
        if _ts_space_id:
            return _ts_space_id
    # (slow I/O happens outside the lock)
    ...
    if results:
        with _space_id_lock:
            _ts_space_id = str(results[0]["id"])
    with _space_id_lock:
        return _ts_space_id
```

Pattern: lock → check → unlock → fetch → lock → write → unlock → lock → read → unlock. Never hold a lock during I/O.

---

### Fix 6: Log exceptions instead of silently swallowing them

**File:** `tools/confluence.py`

```python
# Before
except Exception:
    pass

# After
except Exception as e:
    logger.warning("Failed to fetch TS space ID: %s", e)
```

---

### Fix 7: Use get_running_loop() inside async functions

**File:** `agent.py`, line 369

```python
# Before
loop = asyncio.get_event_loop()

# After
loop = asyncio.get_running_loop()
```

Inside an `async def`, there is always a running loop. `get_running_loop()` returns it directly and raises `RuntimeError` immediately if called outside a coroutine (making bugs obvious). `get_event_loop()` is deprecated in Python 3.10+ and will break in future versions.

---

### Fix 8: Use .get() for optional fields from external API responses

**File:** `agent.py`, line 389

```python
# Before
f"/wiki/pages/{created['id']}"

# After
f"/wiki/pages/{created.get('id', 'unknown')}"
```

## Why This Works

**CQL escaping:** Confluence CQL treats `\"` as an escaped literal quote. By replacing every `"` with `\"` before interpolation, the value can never terminate the enclosing string literal or inject new CQL tokens.

**HTML sanitization symmetry:** Applying `_strip_dangerous_html` to both halves of the concatenated body means the final stored value is always the union of two sanitized strings, regardless of what a Confluence editor previously stored.

**is_closed() vs SELECT 1:** `is_closed()` reads an in-memory flag — the lock is held for nanoseconds, not hundreds of milliseconds. Concurrent callers effectively pass through without contention on a healthy connection.

**Context manager cursor lifecycle:** `with` calls `__exit__` unconditionally on both normal and exceptional exits — equivalent to `try/finally: cur.close()` but harder to accidentally omit in a future edit.

**Lock-protected module cache:** `threading.Lock` provides mutual exclusion without relying on GIL atomicity guarantees. The pattern releases the lock before the slow Confluence fetch so lock contention does not become a throughput bottleneck.

**get_running_loop() correctness:** Always returns the correct loop when called inside a coroutine; raises `RuntimeError` immediately if called outside one — making misuse loud rather than silent.

## Prevention

1. **Parameterize or escape all query strings at the call site.** Treat CQL like SQL. Consider a `cql_escape(value: str) -> str` helper that centralizes the escaping and can be extended if the API adds more metacharacters. Add a unit test with values containing `"`, `\`, `AND`, and `OR`.

2. **Apply sanitization symmetrically.** If you sanitize data going _out_ to an API, sanitize data coming _in_ from that API before round-tripping it. Audit: grep for `existing_*` or `fetched_*` variables being concatenated or stored.

3. **Never perform I/O inside a held lock.** The invariant: locks protect in-memory state; I/O happens outside locks. Pattern: (1) lock, check, unlock; (2) fetch; (3) lock, write, unlock.

4. **Always use cursors as context managers.** Add a pre-commit check: `grep -n 'conn\.cursor(' tools/snowflake.py | grep -v 'with '` should return no results. Same applies to file handles and HTTP sessions.

5. **Protect module-level mutable state with locks.** Any `global` that is not `Final` needs a `threading.Lock`. Consider `@functools.lru_cache(maxsize=1)` for simple caches that don't need manual invalidation — it is thread-safe by default.

6. **Never write `except Exception: pass`.** Minimum: `except Exception as e: logger.warning(...)`. Enforce with `flake8-bugbear` rule B110.

7. **Audit asyncio API usage before Python version bumps.** Run `grep -rn 'get_event_loop\|get_or_create_eventloop' .` before upgrading. `get_running_loop()` is correct inside coroutines; `asyncio.run()` is correct at the top-level entry point.

8. **Treat external API responses as untrusted dicts.** Use `.get(key, default)` instead of `[key]` when the response shape is not guaranteed by a typed schema. If you want to fail loudly, raise a descriptive `ValueError` rather than letting Python raise an opaque `KeyError`.

## Related Issues

- Commit `a13ee89` — `fix(review): apply P1/P2 findings from code review`
- Prior commit `92339db` — `feat: dynamic Confluence space routing for search and publish` (introduced the routing logic that CQL injection and _ts_space_id issues were found in)
