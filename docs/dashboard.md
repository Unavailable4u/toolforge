# The operator dashboard

`toolforge dashboard` serves a small web page for the person who runs ToolForge. It shows the registry, why a tool was accepted or
rejected, the permissions an approval request asks for, the Blast Radius report, the live event stream and the sessions, and it lets
the operator approve, deny, promote, quarantine, release, roll back and close a session. It is FastAPI plus one static page of plain
JavaScript. There is no build step and no other dependency.

```text
toolforge dashboard                       # http://127.0.0.1:8787/
toolforge dashboard --port 9000 --runs-dir ./runs
```

It is a **separate process** from `toolforge serve`. Both read the same registry and `TOOLFORGE_HOME`. The MCP server does not import the
dashboard and an agent has no route to it: an agent can ask for approval, only a person can give it.

## Who may reach it

| Setting | Default | Meaning |
| --- | --- | --- |
| `TOOLFORGE_DASHBOARD_HOST` | `127.0.0.1` | Address to bind. Anything that is not loopback needs the token or the command **refuses to start**. |
| `TOOLFORGE_DASHBOARD_PORT` | `8787` | Port. |
| `TOOLFORGE_DASHBOARD_TOKEN` | none | Bearer token for everything under `/api`. Read from the environment only, never printed. |
| `TOOLFORGE_DASHBOARD_ALLOWED_HOSTS` | none | Extra `Host` values (comma separated), for another name or a reverse proxy. |
| `TOOLFORGE_DASHBOARD_ALLOWED_ORIGINS` | none | Extra `Origin` values (comma separated). |

The rules are the ones the MCP listener uses (`toolforge.netguard`, see [clients.md](clients.md)): the `Host` header must be one the listener
expects, an `Origin` header must be a loopback origin or a configured one, and the token comparison takes the same time wherever the first
difference is. Two things differ because a browser is the client:

* **The page loads without the token, the data does not.** A browser cannot attach a bearer header to a page load, so `/` and
  `/static/*` are served to anyone who passes the `Host` and `Origin` checks. They contain no server data. Everything under `/api` needs
  the token when one is set, and the routes check it again themselves. The page asks for the token, keeps it for that tab only, and sends it in
  an `Authorization` header.
* **There are no cookies.** With no cookie, a page on another origin has nothing to ride on. A request that changes state must also be
  `application/json` (anything else gets `415`), and a foreign `Origin` is refused whether or not a token is set.

There is no encryption. Beyond a trusted network, put a TLS-terminating proxy in front of it. A loopback address protects against other
machines, not against other users or programs on this one.

## What is on the page

* **Registry**: every tool with its active version, how many versions are in each status, the success rate and a description; search by
  name or description and filter by status. A success rate with fewer than five recent calls is drawn hatched and says `not measured`, never `0%`.
* **Tool**: versions with their kind, who made them, the lineage (which version was derived from which), and a link to each version.
* **Version**: its kind, permissions (network, secrets, and whether it drives a browser), the steps of a browser plan, the gates with their summaries and hints, the gate history, the stored Blast Radius report (summary, and the full
  page inside a sandboxed frame), recent calls, a **Verify the hash chain** button that re-reads the ledger files a page at a time and checks each
  chain, and the operator actions that apply to its status.
* **Approvals**: the permission diff first (what the version adds to, keeps from and removes from the active one), then, for a browser plan, every step it will run, then the gates, the skipped
  gates, whether each brokered secret has a broker, and what the network allowance does not stop. The diff is the decision, not the description.
* **Sessions**: open and closed sessions by public name, and a **Close** button.
* **Attack lab** and **Benchmark**: the newest stored result (see below). An attack run is drawn from the file `toolforge attacks run --json` writes: the counts, then one row per attack with its verdict, its evidence and its checks. Only `contained` is green; `breached` is red, and `inconclusive` and `skipped` stay amber, because an attack that could not be judged is not a defence that held.
* **Live**: new ledger events and finished calls as they happen.

A report that was never stored, a ledger that cannot be verified here and a number that was not measured are shown as gaps. A tick never
stands for something that was not checked.

## What it will not do

* **It never starts a sandbox.** Approving a version whose manifest has live examples has to run them once in a sandbox. The dashboard
  refuses that approval (the version stays `pending_approval`, nothing is written) and the page says to use `toolforge approvals approve`.
  Denying needs no sandbox and works here.
* **It never starts an attack run or a benchmark.** They are command-line tools. The Attack lab and Benchmark pages read what they stored:
  `<runs>/attacks/attacks-<time>.json` and `<runs>/bench/<run>/summary.json`, where `<runs>` is `./runs` or `--runs-dir`. With no file they say
  `no results yet`. There is no endpoint that runs one.
* **It never shows the secret half of a session id.** Calls and sessions are shown by public id only.

## Same functions, same refusals

Every write calls the function the command line calls (`toolforge.services.operator_actions`), so the same refusals apply and the same ledger
events are written, by the same actor (`operator`):

| Page action | Request | Function | Refused when |
| --- | --- | --- | --- |
| Approve, Deny | `POST /api/approvals/{id}` `{decision, reason}` | `decide` | no reason; request already decided; package changed since the request; a secret with no broker; a live example needs a sandbox |
| Promote | `POST /api/versions/{name}/{version}/promote` `{reason}` | `promote` | no reason; status is not `verified_basic` or `verified`; the manifest asks for network or secrets (that is approval) |
| Quarantine | `POST /api/tools/{name}/quarantine` `{reason, version?}` | `quarantine` | not `active` (the version defaults to the active one) |
| Release | `POST /api/tools/{name}/release` `{reason, version}` | `release` | not `quarantined` |
| Roll back | `POST /api/tools/{name}/rollback` `{reason, version}` | `rollback` | never active, or needs an approval it does not have |
| Close | `POST /api/sessions/{id}/close` | `cli_sessions.close_one` | unknown session (a close of a live session is requested and the owning server closes it) |

A reason is required and is written to the ledger. A refusal is `409` (state), `404` (unknown), `400` or `422` (a bad request). When the registry
changed but the ledger line could not be written, the answer is `200` with `ledger_written: false` and a warning, because the change is made and
a retry would be refused: the registry is the truth, the ledger is the history. Overdue approval requests are denied whenever the list or a
request is read, exactly as every `toolforge approvals` command does first; it can only say no.

## The API

All under `/api`; the token rule above applies.

| Route | Answer |
| --- | --- |
| `GET /meta` | version, lifecycle statuses, the gates in plain language, `token_required` |
| `GET /tools?q=&status=&limit=&offset=` | tools |
| `GET /tools/{name}` | versions and lineage |
| `GET /versions/{name}/{version}` | manifest, permissions, gates with hints, gate history, stats, flags, available actions, recent calls |
| `GET /versions/{name}/{version}/calls?limit=&before=` | calls, newest first, paged backwards |
| `GET /versions/{name}/{version}/report?format=json\|html` | the stored Blast Radius report (never generated here) |
| `GET /ledger/{name}/{version}/verify?limit=&offset=` | hash-chain check of one page of ledger files |
| `GET /approvals?status=` and `GET /approvals/{id}` | requests, with the permission diff |
| `GET /sessions?state=open\|all` | sessions |
| `GET /attacks`, `GET /bench` | stored results |
| `GET /events?replay=&max_events=` | server-sent events: `ready`, then `ledger` and `call` events |

The event stream is built from the registry's `calls` table and from the ledger files, read from where the last poll stopped. An event is a
summary (type, actor, time, tool, a few short fields); payloads, call arguments and call results never go on it. A browser's `EventSource` cannot send
a header, so the page reads the stream with `fetch`.

## Text that a tool wrote

A description, a gate hint or a report line is text from a stranger. The server removes invisible and control characters and cuts long text before it leaves
(the same function the MCP server uses), and the page writes it with `textContent` only. `tests/test_dashboard_static.py` fails if the script ever
gains an HTML sink (`innerHTML`, `insertAdjacentHTML`, `document.write`, `eval` and the rest). The one place server HTML is shown, MiniLocker's own
rendering of the Blast Radius report, goes into a frame with an empty `sandbox` attribute and is served with a policy that allows no script. The page's
content-security policy allows scripts from itself only and has no inline script. A description the injection scanner suspects carries a `flagged` badge.

## Limits

* Ledger events on the live page come from at most the 5,000 most recently changed ledger files per poll; call completions come from the registry and are
  not affected. Ledger verification reads at most 8 MB per file and 100 files per request.
* The page was checked by reading its source, by `node --check`, and by the tests above, which drive the API. It has not been exercised in a real
  browser; the planned browser tests (P19, P20) do that.
* One dashboard process, one operator. There are no accounts and no audit of who clicked, only the `operator` actor in the ledger.

Proven by `tests/test_dashboard_api.py`, `tests/test_dashboard_security.py`, `tests/test_dashboard_events.py`, `tests/test_dashboard_static.py`,
`tests/test_dashboard_results.py`, `tests/test_registry_calls.py` and the `token_paths` tests in `tests/test_netguard.py`.
