// ToolForge dashboard. Everything that came from a tool author or from the server is put on the page with textContent, never as HTML:
// there are no HTML-string sinks in this file (tests/test_dashboard_static.py keeps it that way).
// The one place server HTML is shown is the Blast Radius page, which goes into an iframe with an empty sandbox attribute (no scripts,
// no same-origin access).
(() => {
  'use strict';
  const main = document.getElementById('main');
  let token = '';
  try { token = sessionStorage.getItem('toolforge_dashboard_token') || ''; } catch (e) { token = ''; }
  let stream = null; // the AbortController of the live feed, when one is open

  // ---- DOM helpers ---------------------------------------------------------------------------------------------------------
  function el(tag, attrs, ...kids) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value === undefined || value === null || value === false) continue;
      if (key === 'class') node.className = value;
      else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? '' : String(value));
    }
    for (const kid of kids.flat(Infinity)) {
      if (kid === undefined || kid === null || kid === false) continue;
      node.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    }
    return node;
  }
  const clear = (node) => { while (node.firstChild) node.removeChild(node.firstChild); };
  const show = (...nodes) => { clear(main); main.append(...nodes.flat(Infinity).filter(Boolean)); };
  const link = (text, hash) => el('a', { href: hash }, text);
  const enc = encodeURIComponent;
  const badge = (text, kind) => el('span', { class: 'badge s-' + (kind || text) }, text);
  const flagBadge = (row) => row && row.flagged ? el('span', { class: 'badge flag', title: 'the injection scanner suspects this text' }, 'flagged') : null;
  const notMeasured = (why) => el('span', { class: 'nm', title: why || 'not measured' }, 'not measured');
  function rate(stats) {
    if (!stats || !stats.measured) return notMeasured('fewer than 5 recent calls');
    return (Math.round(stats.success_rate * 1000) / 10) + '% of ' + stats.recent_calls;
  }
  const when = (iso) => iso || '';
  function table(head, rows) {
    return el('div', { class: 'wrap' }, el('table', {},
      el('thead', {}, el('tr', {}, head.map((h) => el('th', {}, h)))),
      el('tbody', {}, rows.map((r) => el('tr', {}, r.map((c) => el('td', {}, c)))))));
  }

  // ---- API -----------------------------------------------------------------------------------------------------------------
  class ApiError extends Error { constructor(info, status) { super(info && info.message || 'request failed'); this.info = info || {}; this.status = status; } }
  async function api(path, opts) {
    const o = opts || {};
    const headers = { Accept: o.text ? 'text/html' : 'application/json' };
    if (token) headers.Authorization = 'Bearer ' + token;
    let body;
    if (o.json !== undefined) { headers['Content-Type'] = 'application/json'; body = JSON.stringify(o.json); }
    const res = await fetch('/api' + path, { method: o.method || 'GET', headers, body, credentials: 'omit', cache: 'no-store', signal: o.signal });
    if (res.status === 401) { askToken(); throw new ApiError({ code: 'unauthorized', message: 'a token is needed' }, 401); }
    if (o.stream && res.ok) return res;
    const text = await res.text();
    let data = null;
    if (!o.text) { try { data = text ? JSON.parse(text) : null; } catch (e) { data = null; } }
    if (!res.ok) throw new ApiError(data && data.error ? data.error : { code: 'http_' + res.status, message: 'the dashboard answered ' + res.status }, res.status);
    return o.text ? text : data;
  }
  function problem(err) {
    const info = err.info || { message: String(err.message || err) };
    return el('div', { class: 'notice bad' }, (info.code ? '[' + info.code + '] ' : '') + info.message, info.hint ? el('div', { class: 'muted' }, info.hint) : null);
  }

  function askToken() {
    const input = el('input', { type: 'password', placeholder: 'dashboard token', autocomplete: 'off' });
    const go = () => { token = input.value.trim(); try { sessionStorage.setItem('toolforge_dashboard_token', token); } catch (e) { /* kept in memory only */ } route(); };
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') go(); });
    show(el('h2', {}, 'Token needed'),
      el('p', {}, 'This dashboard asks for the value of TOOLFORGE_DASHBOARD_TOKEN. It is kept for this tab only and sent in an Authorization header, never in a cookie or a URL.'),
      el('div', { class: 'row' }, input, el('button', { class: 'primary', onclick: go }, 'Continue')));
  }

  // ---- operator actions ----------------------------------------------------------------------------------------------------
  // A reason is required, as on the command line, and is written to the ledger. The server decides whether the action is allowed.
  function actionBox(title, verb, danger, send) {
    const reason = el('textarea', { placeholder: 'Why? This is written to the ledger.', 'aria-label': 'reason' });
    const out = el('div', {});
    const button = el('button', { class: danger ? 'danger' : 'primary' }, verb);
    button.addEventListener('click', async () => {
      const text = reason.value.trim();
      if (!text) { clear(out); out.append(el('div', { class: 'notice bad' }, verb + ' needs a reason.')); return; }
      button.disabled = true; clear(out);
      try {
        const done = await send(text);
        out.append(el('div', { class: 'notice ' + (done.warning ? '' : 'ok') }, done.text, done.warning ? el('div', {}, done.warning) : null));
        setTimeout(route, 900);
      } catch (err) { out.append(problem(err)); button.disabled = false; }
    });
    return el('div', { class: 'card' }, el('strong', {}, title), reason, el('div', { class: 'row' }, button), out);
  }
  const post = (path, json) => api(path, { method: 'POST', json });

  // ---- views ---------------------------------------------------------------------------------------------------------------
  async function viewTools() {
    const q = el('input', { type: 'search', placeholder: 'search name or description', 'aria-label': 'search' });
    const status = el('select', { 'aria-label': 'status' }, el('option', { value: '' }, 'any status'));
    const meta = await api('/meta');
    for (const s of meta.statuses) status.append(el('option', { value: s }, s));
    const results = el('div', {});
    const load = async () => {
      try {
        const data = await api('/tools?q=' + enc(q.value) + '&status=' + enc(status.value));
        clear(results);
        if (!data.tools.length) { results.append(el('p', { class: 'muted' }, 'No tools match.')); return; }
        results.append(table(['tool', 'kind', 'active', 'versions', 'success', 'description'], data.tools.map((t) => [
          link(t.name, '#/tools/' + enc(t.name)), t.kind || '',
          t.active_version ? t.active_version : el('span', { class: 'muted' }, 'none'),
          Object.entries(t.status_counts).map(([s, n]) => el('span', {}, badge(s), ' ' + n + ' ')),
          rate(t.stats),
          [flagBadge(t), ' ', t.description],
        ])));
      } catch (err) { clear(results); results.append(problem(err)); }
    };
    q.addEventListener('input', () => { clearTimeout(q._t); q._t = setTimeout(load, 250); });
    status.addEventListener('change', load);
    show(el('h2', {}, 'Registry'), el('div', { class: 'row' }, q, status), results);
    await load();
  }

  async function viewTool(name) {
    const t = await api('/tools/' + enc(name));
    show(el('h2', {}, t.name), el('p', {}, 'active version: ', t.active_version || el('span', { class: 'muted' }, 'none')),
      table(['version', 'status', 'kind', 'derived from', 'created', 'by', 'success'], t.versions.map((v) => [
        link(v.version, '#/versions/' + enc(t.name) + '/' + enc(v.version)), badge(v.status), v.kind || '', v.parent_version || '', when(v.created_at), v.created_by, rate(v.stats)])),
      el('h3', {}, 'Lineage'),
      el('ul', { class: 'plain' }, t.lineage.map((l) => el('li', {}, l.parent ? l.parent + ' → ' : 'first → ', l.version, ' ', badge(l.status)))));
  }

  function permList(perms) {
    const rows = [];
    for (const kind of ['network', 'secrets']) rows.push([kind, perms[kind] && perms[kind].length ? perms[kind].join(', ') : el('span', { class: 'muted' }, 'none')]);
    rows.push(['browser', perms.browser ? 'drives a headless browser through the egress proxy' : el('span', { class: 'muted' }, 'no')]);
    return table(['permission', 'asked for'], rows);
  }

  async function viewVersion(name, version) {
    const base = '/versions/' + enc(name) + '/' + enc(version);
    const [v, rep] = await Promise.all([api(base), api(base + '/report')]);
    const sections = [];
    sections.push(el('p', {}, link(v.name, '#/tools/' + enc(v.name)), ' @ ' + v.version + ' ', badge(v.status), ' ', el('span', { class: 'badge' }, v.kind), ' ', flagBadge(v)));
    sections.push(el('p', {}, v.description));
    if (v.when_to_use) sections.push(el('p', { class: 'muted' }, 'when to use: ' + v.when_to_use));
    sections.push(el('p', {}, 'code ', el('span', { class: 'mono' }, v.code_sha.slice(0, 12)), ' · created ', when(v.created_at), ' by ', v.created_by, ' · success ', rate(v.stats), ' · open sessions ', String(v.open_sessions)));
    if (v.next_action) sections.push(el('div', { class: 'notice' }, v.next_action));
    sections.push(el('h3', {}, 'Permissions'), permList(v.permissions));
    if (v.plan) sections.push(el('h3', {}, 'Browser plan steps'), el('ol', {}, v.plan.map((line) => el('li', { class: 'mono' }, line.replace(/^\d+\.\s*/, '')))));
    if (v.approval) sections.push(el('p', {}, 'approval request ', link('#' + v.approval.id, '#/approvals/' + v.approval.id), ' ', badge(v.approval.status)));

    sections.push(el('h3', {}, 'Gates'));
    sections.push(table(['gate', 'status', 'what it found'], v.gates.map((g) => [g.id + ' ' + g.name, badge(g.status), [g.summary, g.hints.length ? el('ul', {}, g.hints.map((h) => el('li', {}, h))) : null]])));

    sections.push(el('h3', {}, 'Blast Radius'));
    const s = rep.summary;
    sections.push(el('p', {}, rep.stored ? badge(s.status, s.status === 'contained' ? 'ok' : 'bad') : notMeasured('no report stored'), ' ', s.summary));
    if (s.violations && s.violations.length) sections.push(el('ul', {}, s.violations.map((x) => el('li', {}, typeof x === 'string' ? x : JSON.stringify(x)))));
    if (s.not_measured && s.not_measured.length) sections.push(el('p', {}, 'not measured: ', s.not_measured.map((x) => el('span', { class: 'nm' }, x + ' '))));
    sections.push(el('p', {}, 'ledger verified: ', s.ledger_verified ? badge('yes', 'ok') : notMeasured('not verified in this report')));
    if (rep.stored) {
      const holder = el('div', {});
      sections.push(el('button', { onclick: async () => {
        clear(holder);
        try {
          const html = await api(base + '/report?format=html', { text: true });
          const frame = el('iframe', { class: 'report', sandbox: '', title: 'Blast Radius report' });
          frame.srcdoc = html;
          holder.append(frame);
        } catch (err) { holder.append(problem(err)); }
      } }, 'Show the full report'), holder);
    }

    sections.push(el('h3', {}, 'Ledger'));
    const ledger = el('div', {});
    sections.push(el('button', { onclick: async () => {
      clear(ledger);
      try {
        const r = await api(base.replace('/versions/', '/ledger/') + '/verify');
        if (!r.measured) { ledger.append(el('p', {}, notMeasured(r.reason), ' ', r.reason)); return; }
        ledger.append(el('p', {}, r.files_total + ' ledger file(s); showing ' + r.files.length), table(['file', 'events', 'chain', 'note'], r.files.map((f) => [f.file, String(f.events), f.chain_ok ? badge('intact', 'ok') : badge('broken', 'bad'), f.problem || f.message])));
      } catch (err) { ledger.append(problem(err)); }
    } }, 'Verify the hash chain'), ledger);

    sections.push(el('h3', {}, 'Recent calls'));
    sections.push(v.recent_calls.length ? table(['time', 'result', 'ms', 'session'], v.recent_calls.map((c) => [when(c.ts), c.ok ? badge('ok') : badge(c.error_class || 'failed', 'bad'), String(c.duration_ms), c.session || ''])) : el('p', { class: 'muted' }, 'No calls recorded.'));

    sections.push(el('h3', {}, 'Gate history'));
    sections.push(table(['time', 'gate', 'status', 'summary'], v.gate_history.map((g) => [when(g.finished_at), g.gate, badge(g.status), g.summary])));

    sections.push(el('h3', {}, 'Operator actions'));
    const n = enc(v.name);
    if (!v.actions.length) sections.push(el('p', { class: 'muted' }, 'Nothing to do from this status.'));
    if (v.actions.includes('promote')) sections.push(actionBox('Promote to active', 'Promote', false, (reason) => post('/versions/' + n + '/' + enc(v.version) + '/promote', { reason })));
    if (v.actions.includes('quarantine')) sections.push(actionBox('Quarantine this version', 'Quarantine', true, (reason) => post('/tools/' + n + '/quarantine', { version: v.version, reason })));
    if (v.actions.includes('release')) sections.push(actionBox('Release from quarantine (read the report first)', 'Release', false, (reason) => post('/tools/' + n + '/release', { version: v.version, reason })));
    if (v.actions.includes('rollback')) sections.push(actionBox('Roll back to this version', 'Roll back', false, (reason) => post('/tools/' + n + '/rollback', { version: v.version, reason })));
    if (v.actions.includes('decide') && v.approval) sections.push(el('p', {}, link('Decide on approval request #' + v.approval.id, '#/approvals/' + v.approval.id)));
    show(el('h2', {}, v.label), sections);
  }

  async function viewApprovals() {
    const data = await api('/approvals?status=all');
    show(el('h2', {}, 'Approvals'), data.approvals.length ? table(['#', 'tool', 'state', 'asked for', 'created'], data.approvals.map((a) => [
      link('#' + a.id, '#/approvals/' + a.id), a.label || '', badge(a.status),
      a.requested && a.requested.requested ? ['network: ' + ((a.requested.requested.network || []).join(', ') || 'none'), ' · secrets: ' + ((a.requested.requested.secrets || []).join(', ') || 'none')] : '',
      when(a.created_at)])) : el('p', { class: 'muted' }, 'No approval requests.'));
  }

  function diffRows(diff) {
    const out = [];
    for (const kind of ['network', 'secrets']) {
      const d = diff && diff[kind] ? diff[kind] : { added: [], kept: [], removed: [] };
      out.push(el('div', { class: 'diff' }, el('strong', {}, kind + ': '),
        d.added.map((x) => el('span', { class: 'add' }, '+ ' + x + ' ')),
        d.kept.map((x) => el('span', { class: 'keep' }, '= ' + x + ' ')),
        d.removed.map((x) => el('span', { class: 'rem' }, '− ' + x + ' ')),
        !d.added.length && !d.kept.length && !d.removed.length ? el('span', { class: 'muted' }, 'none') : null));
    }
    return out;
  }

  async function viewApproval(id) {
    const a = await api('/approvals/' + enc(id));
    const r = a.requested || {};
    const parts = [el('h2', {}, 'Approval request #' + a.id + ' · ' + (a.label || '')), el('p', {}, badge(a.status), ' version is ', badge(a.version_status || 'unknown'), a.decided_by ? ' · decided by ' + a.decided_by + ' ' + when(a.decided_at) : '')];
    parts.push(el('h3', {}, 'What it asks for' + (r.permission_diff && r.permission_diff.against ? ' (compared with ' + r.permission_diff.against + ')' : ' (no active version to compare with)')));
    parts.push(el('div', { class: 'card' }, diffRows(r.permission_diff)));
    if (r.description) parts.push(el('p', {}, r.description_scan && r.description_scan.suspected ? el('span', { class: 'badge flag' }, 'flagged') : null, ' ', r.description));
    if (r.brokers && Object.keys(r.brokers).length) parts.push(table(['secret', 'broker'], Object.entries(r.brokers).map(([k, b]) => [k, b.configured ? 'configured' : el('span', { class: 'nm' }, 'not configured')])));
    if (r.live_examples) parts.push(el('div', { class: 'notice' }, r.live_examples + ' live example(s). Approving them needs a sandbox, which a web request does not start: approve from the command line with `toolforge approvals approve`.'));
    if ((r.skipped_gates || []).length) parts.push(el('div', { class: 'notice' }, 'skipped gates: ' + r.skipped_gates.join(', ')));
    if (r.gates) parts.push(table(['gate', 'status', 'summary'], r.gates.map((g) => [g.id, badge(g.status), g.summary || ''])));
    if (r.plan && r.plan.length) parts.push(el('h3', {}, 'Browser plan steps (what you are approving)'), el('ol', {}, r.plan.map((line) => el('li', { class: 'mono' }, line.replace(/^\d+\.\s*/, '')))));
    if (r.limits && r.limits.length) parts.push(el('h3', {}, 'What the network allowance does not stop'), el('ul', {}, r.limits.map((x) => el('li', {}, x))));
    if (a.status === 'pending') {
      const n = enc(String(a.id));
      parts.push(el('h3', {}, 'Decision'),
        actionBox('Approve: the version becomes active with exactly these permissions', 'Approve', false, (reason) => post('/approvals/' + n, { decision: 'approve', reason })),
        actionBox('Deny: the version is rejected', 'Deny', true, (reason) => post('/approvals/' + n, { decision: 'deny', reason })));
    }
    show(parts);
  }

  async function viewSessions() {
    const data = await api('/sessions?state=all');
    const rows = data.sessions.map((s) => {
      const close = s.state === 'open' ? el('button', { class: 'danger', onclick: async (e) => {
        e.target.disabled = true;
        try { const d = await post('/sessions/' + enc(s.id) + '/close', {}); e.target.replaceWith(document.createTextNode(d.text)); } catch (err) { e.target.replaceWith(problem(err)); }
      } }, 'Close') : null;
      return [s.id, s.label, badge(s.state), s.steps + ' steps', s.state === 'open' ? s.left_s + ' s left' + (s.orphaned ? ' · server gone' : '') : (s.reason || ''), close];
    });
    show(el('h2', {}, 'Sessions'), rows.length ? table(['id', 'tool', 'state', 'steps', 'detail', ''], rows) : el('p', { class: 'muted' }, 'No sessions.'));
  }

  // Stored attack and benchmark results. An attack run (`toolforge attacks run --json`) is drawn from its fields; anything else is a table
  // for a familiar shape or plain text. Nothing is run from here. Only a `contained` verdict is green: breached is red, and inconclusive and
  // skipped stay amber, because an attack that could not be judged is not a defence that held.
  const VERDICT = { contained: 'ok', breached: 'bad', inconclusive: 'warn', skipped: 'warn' };
  function attackView(doc) {
    const out = [];
    const sum = doc.summary || {};
    const total = doc.results.length;
    out.push(el('p', {}, ['contained', 'breached', 'inconclusive', 'skipped'].map((k) => el('span', {}, el('span', { class: 'badge s-' + VERDICT[k] }, k + ' ' + (sum[k] === undefined ? '?' : sum[k])), ' ')), ' of ' + total));
    out.push(el('p', { class: 'muted' }, 'run ' + (doc.generated_at || '') + ' · ToolForge ' + (doc.toolforge || '') + ' · docker: ' + (doc.docker || 'unknown')));
    out.push(table(['attack', 'verdict', 'evidence', 'seconds'], doc.results.map((r) => [
      [el('div', { class: 'mono' }, r.id), r.title],
      el('span', { class: 'badge s-' + (VERDICT[r.verdict] || 'warn') }, r.verdict),
      [r.evidence, el('details', {}, el('summary', {}, (r.checks || []).length + ' check(s)'), el('ul', {}, (r.checks || []).map((c) => el('li', {}, [el('span', { class: 'badge s-' + (c.status === 'pass' ? 'ok' : c.status === 'fail' ? 'bad' : 'warn') }, c.status), ' ' + c.label + ': ' + c.detail]))))],
      String(r.seconds)])));
    if (doc.footer) out.push(el('p', { class: 'muted' }, doc.footer));
    return out;
  }
  function resultView(doc) {
    if (doc && Array.isArray(doc.results) && doc.results.length && doc.results.every((r) => r && typeof r.verdict === 'string')) return [attackView(doc), el('details', {}, el('summary', {}, 'raw result'), el('pre', {}, JSON.stringify(doc, null, 2)))];
    const out = [];
    if (doc && doc.configs) out.push(table(['config', 'summary'], Object.entries(doc.configs).map(([k, v]) => [k, JSON.stringify(v)])));
    out.push(el('details', {}, el('summary', {}, 'raw result'), el('pre', {}, JSON.stringify(doc, null, 2))));
    return out;
  }
  async function viewResults(kind, title) {
    const d = await api('/' + kind);
    if (!d.available) { show(el('h2', {}, title), el('p', {}, notMeasured('no stored results'), ' ', d.message || 'no results yet'), el('p', { class: 'muted' }, 'Run it from the command line, then reload. The dashboard never starts a run.')); return; }
    show(el('h2', {}, title), el('p', { class: 'muted' }, 'file ' + d.latest.name + ' · ' + d.files.length + ' result file(s) found'), resultView(d.latest.document));
  }

  async function viewLive() {
    const list = el('ul', { class: 'plain live' });
    show(el('h2', {}, 'Live'), el('p', { class: 'muted' }, 'New ledger events and finished calls, as they happen.'), list);
    stream = new AbortController();
    const res = await api('/events?replay=20', { stream: true, signal: stream.signal });
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let cut;
      while ((cut = buffer.indexOf('\n\n')) >= 0) {
        const frame = buffer.slice(0, cut); buffer = buffer.slice(cut + 2);
        const data = frame.split('\n').filter((l) => l.startsWith('data: ')).map((l) => l.slice(6)).join('');
        if (!data || frame.startsWith('event: ready')) continue;
        let ev; try { ev = JSON.parse(data); } catch (e) { continue; }
        const fields = Object.entries(ev.fields || {}).map(([k, v]) => k + '=' + v).join(' ');
        list.prepend(el('li', {}, badge(ev.kind, ev.kind === 'call' ? 'info' : 'verified'), ' ', ev.type, ' ', el('strong', {}, ev.label), ' ', el('span', { class: 'muted' }, (ev.actor ? ev.actor + ' · ' : '') + fields)));
        while (list.children.length > 200) list.removeChild(list.lastChild);
      }
    }
  }

  // ---- router --------------------------------------------------------------------------------------------------------------
  async function route() {
    if (stream) { stream.abort(); stream = null; }
    const parts = (location.hash || '#/tools').replace(/^#\/?/, '').split('/').map((p) => { try { return decodeURIComponent(p); } catch (e) { return p; } });
    const [section, a, b] = parts;
    for (const link_ of document.querySelectorAll('nav a')) link_.classList.toggle('on', link_.dataset.route === section || (section === 'versions' && link_.dataset.route === 'tools'));
    try {
      if (section === 'tools' && a) await viewTool(a);
      else if (section === 'versions' && a && b) await viewVersion(a, b);
      else if (section === 'approvals' && a) await viewApproval(a);
      else if (section === 'approvals') await viewApprovals();
      else if (section === 'sessions') await viewSessions();
      else if (section === 'attacks') await viewResults('attacks', 'Attack lab');
      else if (section === 'bench') await viewResults('bench', 'Benchmark');
      else if (section === 'live') await viewLive();
      else await viewTools();
    } catch (err) {
      if (err.name === 'AbortError' || err.status === 401) return;
      show(el('h2', {}, 'Something went wrong'), problem(err));
    }
  }
  window.addEventListener('hashchange', route);
  api('/meta').then((m) => { document.getElementById('version').textContent = 'v' + m.toolforge_version; }).catch(() => {}).finally(route);
})();
