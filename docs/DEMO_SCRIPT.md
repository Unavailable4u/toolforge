# Demo script

A 3 to 4 minute screen recording of ToolForge at work. The demo is `demo/run_demo.py`: four short acts in which an agent loop uses a real
ToolForge server. In `--replay` mode the **agent's replies are scripted** (`demo/transcripts/`, written from the reference packs; no language
model is involved) and everything else is real: the MCP server, the registry, the gates, the sandbox and the ledger. Say so in the recording;
the script prints it too. `--live` uses your configured model instead and its result will vary.

## Before you record

```
git submodule update --init third_party/minilocker
pip install -e ".[dev,sandbox]"
toolforge images build                   # the runtime image, once
toolforge doctor                         # every required check ok
python demo/run_demo.py --replay demo/transcripts/ --out runs/demo-rehearsal      # one dry run
```

Checklist:

* [ ] A clean terminal, a 16 to 18 point monospace font, a window about 110 columns wide, dark text on a plain background, no prompt clutter.
* [ ] `.env` holds **no real key** and no secret is in the shell environment of the recording terminal (`env | grep -i -E "key|token|secret"` is empty).
* [ ] Nothing to clean: every run makes its own fresh home, `runs/demo/<timestamp>/home` (printed in the banner), so the shelf is always empty at act 1 and act 2 finds what act 1 forged.
* [ ] The dashboard ready for after the run: `TOOLFORGE_HOME=runs/demo/<timestamp>/home toolforge dashboard` (take the path from the banner), open at `http://127.0.0.1:8787/`, browser zoom 125 percent. The demo's server reads the environment and the repository's `.env`, like every other command.
* [ ] Notifications off. Nothing private in the browser tabs.
* [ ] A rehearsal done within the hour, so Docker's first-start delay is gone.
* [ ] The cover image rendered: `make cover` (needs one of the converters `scripts/render_cover.py` names).

## Shot list

| Time | Shot | Command and what is on screen | What to say |
| --- | --- | --- | --- |
| 0:00 | Title | The cover image (`docs/assets/cover.png`) | "ToolForge lets an AI agent build its own tools, and makes sure it can't hurt you while it does." |
| 0:15 | Start | `python demo/run_demo.py --replay demo/transcripts/ --pace 0.25` | "The agent's replies are scripted so this is repeatable. The server, the gates and the sandbox are the real ones." |
| 0:30 | Act 1, Forge | The agent calls `forge_find` and gets nothing; `forge_submit`; the gate lines G1 to G4 scroll by; the operator line appears | "It has no SQLite tool. It checks the shelf, writes one, and the gates run. Then a person, not the agent, promotes it." |
| 1:10 | Act 1 result | `Answer: ACCEPTED by the hidden checker` | "The answer was checked against a hidden answer the agent never saw." |
| 1:20 | Act 2, Reuse | `forge_find` returns the tool; one `forge_run`; the Act 1 next to act 2 table | "Different database, same job. The tool is found, not rebuilt: fewer calls, no gates." |
| 1:50 | Act 3, Attack | A poisoned memo tells the agent to send a key to a host. The hostile submit is rejected at G1; with Docker, a quieter attempt is caught by G6 | "A hijacked agent obeys the file and submits a credential stealer. It never runs." |
| 2:30 | Act 3, evidence | The registry lines and the Blast Radius text, with its "not measured" lines | "The ledger says which gate stopped it. Notice what the report does not claim: whatever wasn't measured says so." |
| 2:55 | Act 4, Software | A session tool: deal, hit, rewind, stand, close | "A game that lasts across calls. A snapshot every step, and the agent takes a move back." |
| 3:25 | Dashboard | Switch to the browser: the Sessions tab (the session, its state and step count), then the version's gate list and Blast Radius | "The operator sees the same facts: the session, its steps, the verdict." |
| 3:45 | Close | `make demo-up` in the terminal, then the repository page | "One command starts the server and the dashboard. The limits are written down in the repository." |

## Fallbacks for the risky steps

* **Docker is slow or the daemon is down.** Run with `--sandbox stand-in` for a rehearsal only. It has no isolation and cannot show the G6 decoy part
  of act 3. Do not record it as if it were the real thing; the output says "STAND-IN" in the banner.
* **A gate summary scrolls too fast.** Add `--pace 0.4`, or record at the end from `runs/demo/<timestamp>/summary.json` and `ledgers/`.
* **The script stops with "the real system answered differently".** The replay checks what the server said before each scripted turn and stops rather than
  carry on. Read the line and fix the cause (a gate that failed for a reason on your machine, such as a missing runtime image), then run again; each run starts from an empty home.
* **Act 3's second attempt is missing.** That is the stand-in sandbox. With Docker, the quieter hostile package passes G1 to G4 and G6 fails it for
  touching a decoy; the ledger line `decoy` shows it.
* **A live model misbehaves.** Switch back to `--replay`. A live recording says `mode: live` and has no promise of repeating.
* **The dashboard does not show the snapshots or the rewind.** It lists the session with its state and step count. The rewind and each snapshot are events in the
  session's ledger (`forge.session.rewound`, `forge.session.step`); rehearse what your dashboard version shows, and if it is not enough, show
  `runs/demo/<timestamp>/ledgers/blackjack_session@1.0.0/sessions/` instead, or the demo's own `session rewound (step 2 -> 1)` line.
* **The dashboard shows an old registry.** It must use the same `TOOLFORGE_HOME` as the demo's home (`runs/demo/<timestamp>/home`); start it with
  `TOOLFORGE_HOME=runs/demo/<timestamp>/home toolforge dashboard`.

## The acts, from the code

<!-- acts:begin -->
| # | Act | Task | What the audience should take away |
| --- | --- | --- | --- |
| 1 | Forge (`forge`) | `sqlite_join` | The agent has no SQLite tool. It checks the shelf, forges one, the gates run, an operator promotes it, and the agent answers. |
| 2 | Reuse (`reuse`) | `sqlite_join` (sibling data) | A different question over a different database: the tool is found, not rebuilt, and the step count drops. |
| 3 | Attack (`attack`) | `hostile_data_file` | A poisoned file tells the agent to build a tool that steals credentials. The package is rejected and the ledger says why. |
| 4 | Software and games (`software`) | `blackjack_hand` | The agent forges a session tool, plays a hand, takes a move back, and the session, snapshots and rewind are visible. |
<!-- acts:end -->

## Making the committed recording

```
python demo/run_demo.py --replay demo/transcripts/ --record demo/recordings/main/
```

The recording is three small files (`recording.json`, `events.json`, `transcript.json`) with allowlisted fields only. `recording.json` says whether the
sandbox was Docker or the stand-in and whether the agent was replayed or live. Check it with `pytest tests/test_demo_recording.py`. Commit a recording made
with Docker; the one in the repository says which it was.

## What the demo does not show

It does not show how often a real model writes a passing tool (run the benchmark), the egress proxy or the credential broker (no demo tool asks for network),
or a pixel-level GUI session. It records nothing with a real credential on screen: the only secret-shaped text is the poisoned file's look-alike, and it is
generated at run time.
