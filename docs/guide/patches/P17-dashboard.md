# P17: Dashboard

Depends on: P09, P12, P13. (Rollback is P12 and sessions are P13; release and quarantine come from P08.)

## Goal

A small web UI for the operator: see the registry, understand why a tool was accepted or rejected, approve, deny or promote,
watch calls live, and open the Blast Radius report. Lean: FastAPI plus one static page with vanilla JavaScript, no build step.

## Scope

`src/toolforge/dashboard/{app.py,static/index.html,static/app.js,static/style.css}`, CLI `toolforge dashboard`, tests with
`TestClient`.

## API (all under `/api`, bearer token required when `dashboard_token` is set; bind to 127.0.0.1 by default)

`GET /tools`, `GET /tools/{name}` (versions, status, lineage), `GET /versions/{name}/{version}` (manifest, gates with summaries
and hints, stats, report links), `GET /versions/{name}/{version}/report` (HTML or JSON), `GET /ledger/{name}/{version}/verify`
(re-verify the hash chain), `GET /approvals`, `POST /approvals/{id}` (`approve` or `deny` with a reason),
`POST /versions/{name}/{version}/promote` (reason required; the same function as `toolforge promote`, P07, with the same refusals),
`POST /tools/{name}/release`, `POST /tools/{name}/rollback`, `POST /tools/{name}/quarantine`, `GET /events` (server-sent events: new
ledger events and call completions), `GET /sessions`, `POST /sessions/{id}/close`, `GET /attacks` and `GET /bench` (read-only views
of the stored result files under `runs/`).

**Attacks and benchmarks are never started from the dashboard.** Running them is heavy, needs Docker and a model, and starting
sandboxes from a web request is the wrong trust shape. They stay on the CLI; the dashboard only displays stored results, so these
tabs need no code from P15 or P16, only their output files, and show "no results yet" when none exist.

The dashboard process shares the registry and data directory with the MCP server but is a separate process with its own
token. It imports the approval and promote functions; the MCP server must not (P09 and P07 tests). It reuses
`netguard` from P07 (loopback test, constant-time bearer check, `Origin` and `Host` allow-list), so the two listeners behave the same.

## UI

Pages (tabs): Registry (search, status chips, success rate), Tool (manifest, permission summary, gate history with hints,
lineage, calls, "verify ledger" button, Blast Radius embedded, a Promote button for `verified_basic` versions that asks for a
reason), Approvals (permission diff front and centre; the diff is the decision, not the description), Live (event stream and
sessions), Attack Lab, Benchmark. Show "not measured" literally, in the hatched style from `site-brief.md`, never as a green tick
or a zero. Never render tool-authored text as HTML: escape everything; descriptions appear as plain text with a "flagged" badge
when scanned.

## Tests

Auth required and enforced; approve, deny and promote change registry state and write ledger events; promote refuses network or
secrets and wrong states; escaping of hostile descriptions; report endpoint returns the stored report; `/attacks` and `/bench`
render stored files and say "no results yet" when absent, and no endpoint starts an attack or a benchmark; SSE emits a test event;
the dashboard refuses to start on a non-loopback host unless a token is set; bad `Origin` is refused.

## Acceptance

`pytest -q`; manual: `toolforge dashboard` and open the Registry and an Approval page.

## Pitfalls

No cookies (token in an `Authorization` header only, avoiding CSRF). No inline scripts that include server data. Large ledgers
are paged, not loaded whole.
