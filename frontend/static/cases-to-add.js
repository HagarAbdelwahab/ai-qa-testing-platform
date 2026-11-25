// "Cases to add" picker for a reconciliation result: tick the uncovered scenarios, give the AIO cycle,
// and add them to AIO (create, link to story, publish, add to the story's test set and the cycle).
// Shared by agents.html and index.html — renders and streams its own job, so the report stays visible.

function renderCasesToAdd(container, issueKey, cases, defaultCycle, heading) {
  if (!cases || !cases.length) return;
  const e = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const box = document.createElement('div');
  box.style.cssText = 'margin-top:14px;padding:12px;border:0.5px solid var(--color-border-secondary);border-radius:8px;background:var(--color-background-secondary)';
  box.innerHTML = `
    <div style="font-size:13px;font-weight:600;margin-bottom:8px">${e(heading || 'Add uncovered scenarios to AIO')} — ${e(issueKey)}</div>
    <div class="cta-list">${cases.map((c, i) => `
      <label style="display:flex;gap:8px;align-items:flex-start;padding:6px 0;border-bottom:0.5px solid var(--color-border-tertiary);font-size:12px;cursor:pointer">
        <input type="checkbox" data-i="${i}" checked style="margin-top:2px"/>
        <span>
          <span style="font-weight:600;color:var(--color-text-primary)">${e(c.title)}</span>${c.row ? ` <span style="color:var(--color-text-secondary)">(row ${e(c.row)})</span>` : ''}
          <details style="margin-top:3px;color:var(--color-text-secondary)"><summary style="cursor:pointer">Details</summary>
            ${c.precondition ? `<div><b>Precondition:</b> ${e(c.precondition)}</div>` : ''}
            <div style="white-space:pre-wrap"><b>Steps:</b> ${e(c.step)}</div>
            ${c.test_data ? `<div><b>Test data:</b> ${e(c.test_data)}</div>` : ''}
            <div><b>Expected:</b> ${e(c.expected_result)}</div>
          </details>
        </span>
      </label>`).join('')}</div>
    <div style="display:flex;gap:8px;align-items:center;margin-top:10px;flex-wrap:wrap">
      <span style="font-size:11px;color:var(--color-text-secondary)">AIO Cycle (optional):</span>
      <input class="cta-cycle" placeholder="e.g. Squad-a-sprint-35" value="${e(defaultCycle || '')}"
        style="background:var(--color-background-primary);border:0.5px solid var(--color-border-secondary);border-radius:4px;color:var(--color-text-primary);font-size:12px;padding:4px 8px;width:190px"/>
      <button class="cta-btn" style="font-size:12px;padding:5px 12px;border-radius:5px;border:0.5px solid var(--color-border-secondary);background:var(--color-background-primary);color:var(--color-text-primary);cursor:pointer">Add selected to AIO</button>
    </div>
    <div class="cta-log" style="display:none;margin-top:8px;font-family:var(--font-mono);font-size:11px;max-height:180px;overflow-y:auto;color:var(--color-text-secondary)"></div>
    <div class="cta-result" style="margin-top:6px;font-size:12px"></div>`;
  container.appendChild(box);

  const btn = box.querySelector('.cta-btn'), cycleEl = box.querySelector('.cta-cycle');
  const log = box.querySelector('.cta-log'), result = box.querySelector('.cta-result');
  btn.onclick = async () => {
    const picked = [...box.querySelectorAll('input[type=checkbox]:checked')].map(cb => cases[+cb.dataset.i]);
    const cycle = cycleEl.value.trim();
    if (!picked.length) { result.textContent = 'Tick at least one case.'; return; }
    btn.disabled = true; log.innerHTML = ''; log.style.display = 'block'; result.textContent = 'Adding…';
    const r = await fetch('/api/agents/add-cases', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({issue_key: issueKey, cycle_name: cycle, cases: picked})});
    const d = await r.json();
    if (!r.ok || d.error) { btn.disabled = false; result.innerHTML = `<span style="color:var(--color-text-danger)">${e(d.error || d.detail || r.statusText)}</span>`; return; }
    const es = new EventSource(`/api/agents/stream/${d.job_id}`);
    es.onmessage = ev => {
      const msg = JSON.parse(ev.data);
      if (msg.type === 'log') {
        const line = document.createElement('div'); line.textContent = msg.message; log.appendChild(line); log.scrollTop = log.scrollHeight;
      } else if (msg.type === 'done' || msg.type === 'error') {
        es.close(); btn.disabled = false;
        const added = msg.result && msg.result.added_cases;
        result.innerHTML = msg.status === 'completed' && added
          ? `<span style="color:var(--color-text-success)">Done:</span> ` + Object.entries(added).map(([t, k]) => `${e(k || '?')} — ${e(t)}`).join('<br>')
          : `<span style="color:var(--color-text-danger)">${e(msg.error || msg.message || 'Failed')}</span>`;
      }
    };
    es.onerror = () => { es.close(); btn.disabled = false; result.textContent = 'Connection lost — check the cycle in AIO before retrying.'; };
  };
}

// Testware drafts (kickoff result): one review form per story. Nothing is in AIO yet —
// "Add selected to AIO" creates only the ticked cases.
function renderDrafts(container, drafts, defaultCycle) {
  const e = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  for (const d of drafts || []) {
    if (d.cases && d.cases.length) {
      renderCasesToAdd(container, d.issue_key, d.cases, defaultCycle, 'Review generated cases, then add to AIO');
    } else {
      container.insertAdjacentHTML('beforeend', `<div style="margin-top:14px;font-size:12px;color:var(--color-text-secondary)">
        ${e(d.issue_key)}: nothing new — all generated cases are already in AIO (${(d.existing || []).length} linked).</div>`);
    }
  }
}
