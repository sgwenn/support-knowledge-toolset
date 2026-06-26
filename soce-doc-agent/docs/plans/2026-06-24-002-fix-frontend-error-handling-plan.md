---
title: "fix: Frontend UX error handling"
date: 2026-06-24
type: fix
depth: standard
---

# fix: Frontend UX error handling

## Summary

The app's three async flows — article generation (SSE), publish (SSE), and digest/coverage (fetch) — fail silently or surface raw JavaScript error messages when things go wrong. A stalled SSE stream hangs indefinitely with no user feedback. A missing API key returns a 403 that looks the same as a network error. Snowflake timeouts manifest as generic fetch failures with no suggestion of what to do.

This plan hardens the frontend error layer across all three flows with: SSE stall detection via heartbeat watchdog, human-readable error classification for the most common failures, and a retry affordance on the digest and coverage tabs.

---

## Problem Frame

| Failure | Current UX | Target UX |
|---------|-----------|-----------|
| SSE stream stalls (no events, no heartbeat) | Spinner hangs forever | Watchdog fires after ~30s, shows actionable message |
| Server down / network error | "Failed to fetch" (raw JS) | "Could not reach the server — is it running?" |
| 403 (missing / wrong API key) | Error banner with HTTP status | "Invalid API key — check the X-Api-Key setting" |
| Snowflake timeout (digest) | "Internal Server Error" text | "Snowflake query timed out — try a narrower component or retry" |
| Digest empty (no tickets) | "No tickets found" card | Unchanged — already handled well |
| Coverage load fails | Raw error message in card | Human-readable with retry button |

---

## Requirements

| ID | Requirement |
|----|-------------|
| R1 | SSE consumers detect stalls (no event + no heartbeat for 30s) and show an actionable error. |
| R2 | HTTP error responses (4xx, 5xx) from `/run`, `/publish`, `/digest`, `/coverage` are translated to human-readable messages. |
| R3 | API key errors (403) display a specific hint directing the user to check their key. |
| R4 | Snowflake timeout responses (504 or body containing "timed out") display a specific retry hint. |
| R5 | Digest and coverage tabs show a Retry button after a failure. |
| R6 | No regression on the success paths for any of the three flows. |
| R7 | Error classification logic lives in one shared helper, not duplicated per flow. |

---

## Key Technical Decisions

**Heartbeat watchdog via `setTimeout` reset**
The server already emits `{"type":"heartbeat"}` every 15 seconds. The client resets a `setTimeout(30s)` on every message (including heartbeats). If 30 seconds pass without any message, the watchdog fires, closes the EventSource, and shows the stall error. This requires no server change.

**`classifyError(response | Error) → string` helper**
A single function checks `response.status`, then `response.text()` for known substrings ("timed out", "Snowflake"), then falls back to a generic message. All four fetch-based calls (`/run` preflight, `/publish` preflight, `/digest`, `/coverage`) and the SSE watchdog path go through this helper. Avoids duplicating message strings across handlers.

**Retry button: re-invoke the existing function**
`runDigest()` and `loadCoverage()` already reset state and kick off the request. The retry button calls them directly — no new abstraction needed.

**SSE close on error**
`EventSource.onerror` already calls `es.close()`. The watchdog path also calls `es.close()` before showing the error so the connection is not leaked.

---

## Scope Boundaries

**In scope**
- `static/app.js`: watchdog, error classification helper, retry buttons, HTTP error translation
- `static/index.html`: retry button markup in digest and coverage tab panels (if needed beyond JS-injected)

**Out of scope**
- Backend error response shape changes (FastAPI exception handlers, structured SSE error events)
- Toast / notification system — error banners already in place, reusing them
- API key management UI (entering/saving the key)
- Retry on article generation (complex — in-flight agent state cannot be restarted client-side)

---

## Implementation Units

### U1. Add `classifyError` helper

**Goal:** One function translates HTTP responses and JS Error objects into user-readable strings for all flows.

**Requirements:** R2, R3, R4, R7

**Dependencies:** none

**Files:** `static/app.js`

**Approach:**
Add `async function classifyError(responseOrError)` near the top of `app.js` (before the flow functions). Logic:

- If the argument is a `Response` object:
  - 403 → `"Invalid API key — check the X-Api-Key setting"`
  - 504 → `"Request timed out — Snowflake may be slow; try a narrower component or retry"`
  - 5xx → read `response.text()`, check for "timed out" / "snowflake" (case-insensitive) → same timeout message; otherwise `"Server error (HTTP ${status}) — check the server logs"`
  - Other 4xx → `"Request rejected (HTTP ${status})"`
- If the argument is an `Error`:
  - `err.message` includes "fetch" or "network" → `"Could not reach the server — is it running?"`
  - Otherwise → `err.message`

**Patterns to follow:** `escapeHtml` helper at the bottom of `app.js` — same style, standalone function.

**Test scenarios:**
- 403 response → returns API key hint string
- 504 response → returns timeout hint string
- 500 response with body "Snowflake timed out" → returns timeout hint string
- 500 response with body "Internal Server Error" → returns generic server error string
- `TypeError("Failed to fetch")` → returns "could not reach the server" string
- `Error("something else")` → returns the raw message

**Verification:** Test by temporarily forcing each error code in the browser console with a mocked `fetch`.

---

### U2. SSE heartbeat watchdog

**Goal:** SSE streams that stall for 30 seconds show an error and close, instead of hanging forever.

**Requirements:** R1

**Dependencies:** U1

**Files:** `static/app.js`

**Approach:**
Extract a `openStream(jobId, onEvent, onError)` helper that wraps EventSource creation. Inside:

```
let watchdog = null
function resetWatchdog() {
  clearTimeout(watchdog)
  watchdog = setTimeout(() => {
    es.close()
    onError("No response from server for 30 seconds — the agent may have stalled")
  }, 30_000)
}
```

Call `resetWatchdog()` on every `es.onmessage` (including heartbeats). Call `clearTimeout(watchdog)` in the `done`/`error` close path and in `es.onerror`.

Replace the inline EventSource setup in `startJob()` and `publishDraft()` with calls to `openStream()`.

**Patterns to follow:** Existing EventSource setup in `startJob()` (lines ~253–265 of `app.js`).

**Test scenarios:**
- Normal completion: watchdog timer is cleared, no error shown.
- Server stops sending (simulate by killing the server mid-stream): error message appears within ~30s and spinner stops.
- Heartbeat arrives at 14s, 28s: watchdog resets each time, no false positive.
- `onerror` fires before the watchdog: only one error path fires (no double-error).

**Verification:** Kill the uvicorn process while a generation is running; confirm the error message appears within 30 seconds.

---

### U3. HTTP error translation for `/run` and `/publish`

**Goal:** Non-2xx responses from the `/run` and `/publish` POST calls show a classified error instead of silently failing or showing a raw status.

**Requirements:** R2, R3, R4

**Dependencies:** U1

**Files:** `static/app.js`

**Approach:**
Currently `startJob()` and `publishDraft()` call `.then(r => r.json())` without checking `r.ok`. Add a guard:

```
.then(async r => {
  if (!r.ok) throw new Error(await classifyError(r))
  return r.json()
})
.catch(err => handleEvent({ type: 'error', message: err.message }))
```

Same pattern for `publishDraft` → `handlePublishEvent`.

**Patterns to follow:** Existing `.catch` in `runDigest()`.

**Test scenarios:**
- `/run` returns 403: error banner shows API key hint.
- `/run` returns 500: error banner shows server error string.
- `/run` returns 200 with `job_id`: normal flow proceeds, no regression.
- Network down when clicking Generate: error banner shows "could not reach the server".

**Verification:** Set a wrong API key in localStorage and click Generate; confirm the specific 403 message appears.

---

### U4. HTTP error translation and retry for `/digest`

**Goal:** Digest failures show a classified error message and a Retry button.

**Requirements:** R2, R3, R4, R5

**Dependencies:** U1

**Files:** `static/app.js`

**Approach:**
In `runDigest()`, update the `.catch` handler to call `await classifyError(err)` and inject a Retry button alongside the error message:

```html
<div class="card">
  <div class="empty-state" style="color:var(--error)">${msg}</div>
  <button class="btn btn-ghost" style="margin-top:8px" onclick="runDigest()">Retry</button>
</div>
```

Also add an `r.ok` guard before `.then(data => ...)` so non-2xx responses go through `classifyError` instead of crashing on `data.tickets`.

**Patterns to follow:** Existing error injection in `runDigest()`.

**Test scenarios:**
- Digest returns 504: classified timeout message shown, Retry button visible.
- Retry button clicked: spinner resets, new request fires.
- Digest returns 200 with empty tickets: existing "No tickets found" card, no Retry button.
- Digest returns 200 with tickets: normal render, no regression.

**Verification:** Temporarily point `SNOWFLAKE_WAREHOUSE` to a nonexistent warehouse; observe classified error and working Retry.

---

### U5. HTTP error translation and retry for `/coverage`

**Goal:** Coverage load failures show a classified error message and a Retry button.

**Requirements:** R2, R3, R5

**Dependencies:** U1

**Files:** `static/app.js`

**Approach:**
Same pattern as U4 applied to `loadCoverage()`. Add `r.ok` guard and update the `.catch` to call `classifyError` and inject a Retry button pointing to `loadCoverage()`.

**Patterns to follow:** Existing error injection in `loadCoverage()`.

**Test scenarios:**
- Coverage returns 403: API key hint + Retry button.
- Coverage returns 500: server error string + Retry button.
- Coverage returns 200 with data: normal render, no regression.
- Retry button clicked: resets button label to "Loading…" and re-fires.

**Verification:** Set a wrong API key; open Coverage tab; confirm classified error and Retry.

---

## Risks & Dependencies

| Risk | Likelihood | Mitigation |
|------|-----------|-----------|
| `classifyError` reads `response.text()` which consumes the body — double-reading breaks the call | Low | Only call `response.text()` inside the error branch (`!r.ok`), never on success paths |
| Watchdog fires during a legitimately slow Snowflake query that's still active | Medium | 30s threshold is above the 25s executor timeout in `main.py`, so the server should always emit a `done` or `error` event first |
| `openStream` refactor breaks existing heartbeat filter (`ev.type !== 'heartbeat'`) | Low | Keep the heartbeat filter in the `onEvent` callback; watchdog reset happens unconditionally on every message before the filter |

---

## Sources & Research

- `static/app.js:229–266` — existing `startJob` / `publishDraft` EventSource setup
- `static/app.js:268–289` — existing `runDigest` with `.catch`
- `static/app.js:344–363` — existing `loadCoverage` with `.catch`
- `main.py:120–147` — server heartbeat emission (15s interval, already implemented)
- `main.py:199–204` — `asyncio.wait_for(timeout=25.0)` for Snowflake — confirms 30s client watchdog won't false-fire on normal slow queries
