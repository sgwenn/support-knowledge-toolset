let currentMode = 'ticket_id';
let currentDraft = null;
let _activeKbStream = null;
let _activePubStream = null;

function apiHeaders(includeContentType = true) {
  const key = localStorage.getItem('kb_agent_api_key') || '';
  const h = { 'X-Api-Key': key };
  if (includeContentType) h['Content-Type'] = 'application/json';
  return h;
}

// U1: Translate HTTP responses and network errors into user-readable strings.
async function classifyError(responseOrError) {
  if (responseOrError && typeof responseOrError.status === 'number') {
    const status = responseOrError.status;
    if (status === 403) return 'Invalid API key — check the X-Api-Key setting';
    if (status === 504) return 'Request timed out — Snowflake may be slow; try a narrower component or retry';
    if (status >= 500) {
      let body = '';
      try { body = await responseOrError.text(); } catch (_) {}
      if (/timed? ?out|snowflake/i.test(body))
        return 'Request timed out — Snowflake may be slow; try a narrower component or retry';
      return `Server error (HTTP ${status}) — check the server logs`;
    }
    return `Request rejected (HTTP ${status})`;
  }
  const msg = (responseOrError && responseOrError.message) || String(responseOrError);
  if (/fetch|network|failed to/i.test(msg)) return 'Could not reach the server — is it running?';
  return msg;
}

// U2: Open an SSE stream with a 30-second stall watchdog.
function openStream(jobId, onEvent, onError) {
  const key = localStorage.getItem('kb_agent_api_key') || '';
  const qs = key ? `?key=${encodeURIComponent(key)}` : '';
  const es = new EventSource(`/stream/${jobId}${qs}`);
  let watchdog = null;

  function resetWatchdog() {
    clearTimeout(watchdog);
    watchdog = setTimeout(() => {
      es.close();
      onError('No response from server for 30 seconds — the agent may have stalled');
    }, 30_000);
  }

  es.onmessage = e => {
    resetWatchdog();
    let ev;
    try { ev = JSON.parse(e.data); } catch (_) { return; }
    onEvent(ev);
    if (ev.type === 'done' || ev.type === 'error') {
      clearTimeout(watchdog);
      es.close();
    }
  };

  es.onerror = () => {
    clearTimeout(watchdog);
    onError('Connection lost');
    es.close();
  };

  resetWatchdog();
  return es;
}

function switchTab(tab, el) {
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('tab-' + tab).classList.add('active');
  el.classList.add('active');
}

function setMode(mode) {
  currentMode = mode;
  document.getElementById('mode-id-btn').classList.toggle('active', mode === 'ticket_id');
  document.getElementById('mode-text-btn').classList.toggle('active', mode === 'raw_text');
  document.getElementById('input-ticket-id').style.display = mode === 'ticket_id' ? '' : 'none';
  document.getElementById('input-raw-text').style.display = mode === 'raw_text' ? '' : 'none';
}

function prefillTicket(id) {
  document.querySelectorAll('.tab-btn')[0].dispatchEvent(new MouseEvent('click', { bubbles: true }));
  setMode('ticket_id');
  document.getElementById('ticket-id-input').value = id;
}

function resetProgress() {
  currentDraft = null;
  document.getElementById('step-list').innerHTML = '';
  document.getElementById('done-banner').classList.remove('visible');
  document.getElementById('error-banner').classList.remove('visible');
  document.getElementById('progress-panel').classList.add('visible');
  document.getElementById('preview-panel').classList.remove('visible');
  document.getElementById('preview-body').innerHTML = '';
  document.querySelector('.generate-layout').classList.remove('has-draft');
  document.getElementById('destination-pill').style.display = 'none';
}

function addStep(iconHtml, label, sub, id) {
  const li = document.createElement('li');
  li.className = 'step';
  if (id) li.id = id;
  li.innerHTML = `
    <div class="step-icon">${iconHtml}</div>
    <div style="flex:1">
      <div class="step-label">${label}</div>
      ${sub ? `<div class="step-sub">${sub}</div>` : ''}
    </div>`;
  document.getElementById('step-list').appendChild(li);
  return li;
}

const PPC_LABELS = {
  agent_ppc:                              'Agent',
  apm_ppc:                               'APM',
  cloud_integrations_ppc:                'Cloud Integrations',
  containers_orchestrators_kubernetes_ppc:'Containers',
  dbm_ppc:                               'Database Monitoring - DBM',
  logs_ppc:                              'Logs',
  metrics_ppc:                           'Metrics',
  monitors_ppc:                          'Monitors (Alerting Platform)',
  new_and_misc_components_ppc:           'New & Misc',
  rum_ppc:                               'RUM',
  security_ppc:                          'Cloud Security Products',
  serverless_ppc:                        'Serverless',
  service_mgmt_ppc:                      'Service Management',
  synthetics_ppc:                        'Synthetics',
  synthetics_rum_ppc:                    'RUM',
  universal_service_monitoring_ppc:      'Universal Service Monitoring',
  web_platform_ppc:                      'Web Platform',
};

function resolveDestination(component) {
  if (!component) return 'TS › KB Drafts';
  const label = PPC_LABELS[component.toLowerCase()] || component;
  return `TS › ${label} › KB Drafts`;
}

const TOOL_LABELS = {
  fetch_ticket_from_snowflake: 'Fetching ticket from Snowflake',
  find_similar_tickets:        'Finding similar tickets',
  search_docs:                 'Searching Datadog docs',
};

const spinnerSVG = `<svg class="spin" width="15" height="15" viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9" stroke="#C4B5FD" stroke-width="2.5"/><path d="M12 3a9 9 0 0 1 9 9" stroke="#6B34C4" stroke-width="2.5" stroke-linecap="round"/></svg>`;
const checkSVG   = `<svg width="15" height="15" viewBox="0 0 24 24" fill="#039855"><path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41L9 16.17z"/></svg>`;
const dotsSVG    = `<svg width="14" height="14" viewBox="0 0 24 24" fill="var(--text-3)"><circle cx="5" cy="12" r="1.8"/><circle cx="12" cy="12" r="1.8"/><circle cx="19" cy="12" r="1.8"/></svg>`;

function handleEvent(event) {
  const { type, name, input_summary, summary, text, result, message } = event;

  if (type === 'tool_call') {
    addStep(spinnerSVG, TOOL_LABELS[name] || name, input_summary, 'step-' + name);
  }

  if (type === 'tool_result') {
    const el = document.getElementById('step-' + name);
    if (el) {
      el.querySelector('.step-icon').innerHTML = checkSVG;
      const sub = el.querySelector('.step-sub');
      if (sub) sub.textContent = summary;
      else el.querySelector('div').insertAdjacentHTML('beforeend', `<div class="step-sub">${summary}</div>`);
    }
  }

  if (type === 'thinking' && text) {
    const li = document.createElement('li');
    li.className = 'step';
    li.innerHTML = `
      <div class="step-icon">${dotsSVG}</div>
      <div style="flex:1">
        <div class="step-thinking" onclick="this.classList.toggle('expanded')">
          <em>Claude is thinking…</em><span class="expand-hint">(expand)</span>
          <div class="thinking-text">${escapeHtml(text)}</div>
        </div>
      </div>`;
    document.getElementById('step-list').appendChild(li);
  }

  if (type === 'done') {
    document.getElementById('run-btn').disabled = false;
    if (result && result.draft_html) {
      currentDraft = result;
      document.getElementById('preview-body').innerHTML = DOMPurify.sanitize(result.draft_html);
      document.getElementById('preview-panel').classList.add('visible');
      document.querySelector('.generate-layout').classList.add('has-draft');
      const notice = document.getElementById('existing-url-notice');
      notice.style.display = result.existing_url ? 'inline' : 'none';
      const pill = document.getElementById('destination-pill');
      document.getElementById('destination-text').textContent = resolveDestination(result.component);
      pill.style.display = '';
    }
  }

  if (type === 'error') {
    const banner = document.getElementById('error-banner');
    banner.classList.add('visible');
    banner.textContent = 'Error: ' + (message || 'Something went wrong');
    document.getElementById('run-btn').disabled = false;
  }
}

function handlePublishEvent(ev) {
  const { type, name, text, result, message } = ev;
  const list = document.getElementById('publish-step-list');

  if (type === 'tool_call') {
    list.style.display = '';
    const li = document.createElement('li');
    li.className = 'step';
    li.id = 'pstep-' + name;
    li.innerHTML = `
      <div class="step-icon">${spinnerSVG}</div>
      <div style="flex:1"><div class="step-label">${escapeHtml(name)}</div></div>`;
    list.appendChild(li);
  }

  if (type === 'tool_result') {
    const el = document.getElementById('pstep-' + name);
    if (el) el.querySelector('.step-icon').innerHTML = checkSVG;
  }

  if (type === 'thinking' && text) {
    list.style.display = '';
    const li = document.createElement('li');
    li.className = 'step';
    li.innerHTML = `
      <div class="step-icon">${dotsSVG}</div>
      <div style="flex:1">
        <div class="step-thinking" onclick="this.classList.toggle('expanded')">
          <em>Claude is thinking…</em><span class="expand-hint">(expand)</span>
          <div class="thinking-text">${escapeHtml(text)}</div>
        </div>
      </div>`;
    list.appendChild(li);
  }

  if (type === 'done') {
    // flip any remaining spinners (e.g. if tool_result wasn't emitted)
    list.querySelectorAll('.step-icon svg.spin').forEach(el => el.parentElement.innerHTML = checkSVG);
    document.getElementById('publish-btn').disabled = false;
    document.getElementById('publish-btn').innerHTML = `
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M22 2L11 13"/><path d="M22 2L15 22 11 13 2 9l20-7z"/></svg>
      Publish to Confluence`;
    if (result && result.confluence_url) {
      document.getElementById('done-banner').classList.add('visible');
      const link = document.getElementById('confluence-link');
      link.href = result.confluence_url;
      link.textContent = result.confluence_url;
    } else {
      const banner = document.getElementById('error-banner');
      banner.classList.add('visible');
      banner.textContent = 'Publish completed but no URL was returned.';
    }
  }

  if (type === 'error') {
    list.querySelectorAll('.step-icon').forEach(el => {
      el.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="var(--error)"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-2h2v2zm0-4h-2V7h2v6z"/></svg>`;
    });
    document.getElementById('publish-btn').disabled = false;
    const banner = document.getElementById('error-banner');
    banner.classList.add('visible');
    banner.textContent = 'Publish error: ' + (message || 'Something went wrong');
  }
}

function publishDraft() {
  if (!currentDraft) return;
  document.getElementById('publish-btn').disabled = true;
  document.getElementById('publish-btn').textContent = 'Publishing…';
  document.getElementById('publish-step-list').innerHTML = '';
  document.getElementById('done-banner').classList.remove('visible');
  document.getElementById('error-banner').classList.remove('visible');

  const email = document.getElementById('email-input').value.trim();

  fetch('/publish', {
    method: 'POST',
    headers: apiHeaders(),
    body: JSON.stringify({
      title: currentDraft.draft_title || 'KB Article',
      draft_html: currentDraft.draft_html,
      existing_url: currentDraft.existing_url || null,
      requester_email: email || null,
      component: currentDraft.component || null,
    }),
  })
    .then(async r => {
      if (!r.ok) throw new Error(await classifyError(r));
      return r.json();
    })
    .then(({ job_id }) => {
      if (_activePubStream) { _activePubStream.close(); _activePubStream = null; }
      _activePubStream = openStream(
        job_id,
        ev => { if (ev.type !== 'heartbeat') handlePublishEvent(ev); },
        msg => handlePublishEvent({ type: 'error', message: msg }),
      );
    })
    .catch(err => handlePublishEvent({ type: 'error', message: err.message }));
}

function discardDraft() {
  currentDraft = null;
  document.getElementById('preview-panel').classList.remove('visible');
  document.querySelector('.generate-layout').classList.remove('has-draft');
}

function startJob() {
  const email = document.getElementById('email-input').value.trim();
  localStorage.setItem('kb_agent_email', email);

  const body = { mode: currentMode, requester_email: email };
  if (currentMode === 'ticket_id') {
    const id = parseInt(document.getElementById('ticket-id-input').value, 10);
    if (!id) return alert('Please enter a ticket ID');
    body.ticket_id = id;
  } else {
    const text = document.getElementById('raw-text-input').value.trim();
    if (!text) return alert('Please paste ticket text');
    body.raw_text = text;
  }

  document.getElementById('run-btn').disabled = true;
  resetProgress();

  fetch('/run', {
    method: 'POST',
    headers: apiHeaders(),
    body: JSON.stringify(body),
  })
    .then(async r => {
      if (!r.ok) throw new Error(await classifyError(r));
      return r.json();
    })
    .then(({ job_id }) => {
      if (_activeKbStream) { _activeKbStream.close(); _activeKbStream = null; }
      _activeKbStream = openStream(
        job_id,
        ev => { if (ev.type !== 'heartbeat') handleEvent(ev); },
        msg => handleEvent({ type: 'error', message: msg }),
      );
    })
    .catch(err => handleEvent({ type: 'error', message: err.message }));
}

function runDigest() {
  const component = document.getElementById('component-input').value.trim();
  const btn = document.getElementById('digest-btn');
  btn.disabled = true;
  btn.textContent = 'Loading…';
  document.getElementById('digest-results').innerHTML = '';

  const url = component ? `/digest?component=${encodeURIComponent(component)}` : '/digest';
  fetch(url, { headers: apiHeaders(false) })
    .then(async r => {
      if (!r.ok) throw new Error(await classifyError(r));
      return r.json();
    })
    .then(data => {
      btn.disabled = false;
      btn.textContent = 'Run Digest';
      renderDigest(data);
    })
    .catch(async err => {
      btn.disabled = false;
      btn.textContent = 'Run Digest';
      const msg = err.message || await classifyError(err);
      document.getElementById('digest-results').innerHTML =
        `<div class="card"><div class="empty-state" style="color:var(--error)">${escapeHtml(msg)}</div>` +
        `<button class="btn btn-ghost" style="margin-top:8px" onclick="runDigest()">Retry</button></div>`;
    });
}

function renderDigest(data) {
  const container = document.getElementById('digest-results');
  if (!data.tickets || !data.tickets.length) {
    container.innerHTML = '<div class="card"><div class="empty-state">No tickets found for the selected period.</div></div>';
    return;
  }
  container.innerHTML = data.tickets.map((t, i) => `
    <div class="digest-card">
      <div class="digest-top">
        <span class="digest-num">0${i + 1}</span>
        <div class="digest-title">${escapeHtml(t.subject || '')}</div>
        <span class="badge ${t.value === 'High value' ? 'badge-high' : 'badge-medium'}">${escapeHtml(t.value || '')}</span>
      </div>
      <div class="digest-meta">#${t.id} · ${escapeHtml(t.component || '')} · ${t.solved_timestamp ? t.solved_timestamp.slice(0,10) : ''}</div>
      <div>${(t.tags||[]).map(tag=>`<span class="tag">${escapeHtml(tag)}</span>`).join('')}</div>
      ${(t.investigation || t.summary) ? `<div class="digest-investigation">${escapeHtml((t.investigation || t.summary).slice(0,400))}${(t.investigation || t.summary).length>400?'…':''}</div>` : ''}
      <div class="digest-actions">
        <button class="btn btn-ghost" style="font-size:12px;padding:5px 10px" onclick="prefillTicket(${t.id})">Generate KB →</button>
      </div>
    </div>
  `).join('');
}

function loadCoverage() {
  const btn = document.getElementById('coverage-btn');
  btn.disabled = true;
  btn.textContent = 'Loading…';
  document.getElementById('coverage-results').innerHTML = '';

  fetch('/coverage', { headers: apiHeaders(false) })
    .then(async r => {
      if (!r.ok) throw new Error(await classifyError(r));
      return r.json();
    })
    .then(data => {
      btn.disabled = false;
      btn.textContent = 'Refresh';
      renderCoverage(data);
    })
    .catch(async err => {
      btn.disabled = false;
      btn.textContent = 'Load Coverage';
      const msg = err.message || await classifyError(err);
      document.getElementById('coverage-results').innerHTML =
        `<div class="card"><div class="empty-state" style="color:var(--error)">${escapeHtml(msg)}</div>` +
        `<button class="btn btn-ghost" style="margin-top:8px" onclick="loadCoverage()">Retry</button></div>`;
    });
}

function renderCoverage(data) {
  const container = document.getElementById('coverage-results');
  const components = data.components || [];
  const timeline = data.weekly_timeline || [];

  if (!components.length && !timeline.length) {
    container.innerHTML = '<div class="card"><div class="empty-state">No coverage data yet — run the digest and publish articles to see progress.</div></div>';
    return;
  }

  const compHtml = components.map(c => {
    const pct = c.gaps > 0 ? Math.min(100, Math.round((c.addressed / c.gaps) * 100)) : 100;
    return `
      <div class="digest-card">
        <div class="digest-top">
          <div class="digest-title">${escapeHtml(c.component)}</div>
          <span class="badge ${pct >= 75 ? 'badge-high' : 'badge-medium'}">${pct}% addressed</span>
        </div>
        <div class="digest-meta">${c.addressed} published · ${c.gaps} gaps identified</div>
        <div class="coverage-bar-bg"><div class="coverage-bar-fill" style="width:${pct}%"></div></div>
      </div>`;
  }).join('');

  const timelineHtml = timeline.length ? `
    <div class="card" style="margin-top:16px">
      <div class="card-title">Weekly Articles Published</div>
      ${timeline.map(w => `
        <div class="digest-meta" style="display:flex;justify-content:space-between;padding:4px 0">
          <span>${escapeHtml(w.week)}</span>
          <span>${w.count} article${w.count !== 1 ? 's' : ''}</span>
        </div>`).join('')}
    </div>` : '';

  container.innerHTML = `<div style="display:grid;gap:12px;margin-top:12px">${compHtml}</div>${timelineHtml}`;
}

function escapeHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

async function checkConfluenceStatus() {
  const container = document.getElementById('confluence-auth');
  if (!container) return;

  const params = new URLSearchParams(window.location.search);
  const authError = params.get('auth_error');
  if (authError) history.replaceState({}, '', window.location.pathname);

  try {
    const resp = await fetch('/auth/status');
    const data = await resp.json();
    if (data.confluence_connected) {
      container.innerHTML = `<span class="confluence-connected-badge"><svg width="7" height="7" viewBox="0 0 8 8" fill="#34D399"><circle cx="4" cy="4" r="4"/></svg>Confluence connected</span>`;
    } else {
      const errorHtml = authError
        ? `<span class="confluence-auth-error">${authError === 'access_denied' ? 'Connection failed — try again' : 'Something went wrong — try again'}</span>`
        : '';
      container.innerHTML = `${errorHtml}<a href="/auth/confluence" id="connect-confluence-btn" class="btn btn-primary" onclick="this.style.pointerEvents='none';this.textContent='Connecting…'"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>Connect Confluence</a>`;
    }
  } catch (_) {}
}

document.addEventListener('DOMContentLoaded', () => {
  checkConfluenceStatus();
  const saved = localStorage.getItem('kb_agent_email');
  if (saved) document.getElementById('email-input').value = saved;
});
