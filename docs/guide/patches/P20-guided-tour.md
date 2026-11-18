# P20: Guided tour

Depends on: P19.

## Goal

A visitor with no background watches one tool get forged, inspected, approved, used, reused and (in the last act) caught
misbehaving, with every step explained in plain words and, on request, in technical detail. The tour plays **real recorded
events** from the demo, so it shows what the software actually did.

## Scope

```
site/src/partials/tour.html  site/src/tour.js  site/src/tour.css
site/src/tour_captions.json   authored captions, keyed by step id: {"simple": "...", "technical": "...", "terms": ["sandbox"]}
site/src/glossary.json        term id -> {"term", "plain", "analogy"}
site/src/tour_illustrative.json   fallback story used only when no recording exists
site/data/tour.json           generated from a recording
scripts/build_tour_data.py    stdlib only
tests/test_tour_*.py  tests/browser/test_tour_browser.py
docs/SITE.md                  gains a "Tour" section: how to re-record and rebuild
```

## The story

One running example. A task arrives that the agent has no tool for (read a made-up binary format described in a spec). Twelve steps:

| # | Step id | What the visitor sees | Plain-language idea |
| --- | --- | --- | --- |
| 1 | `meet` | The agent and its task | An assistant is asked for something it has no tool for. |
| 2 | `find` | `forge_find` returns nothing | It first checks the shelf. Nothing fits. |
| 3 | `forge` | The package: the label (manifest), the code, the tests | It writes a new tool, plus a label saying what the tool may touch. |
| 4 | `read_build` | G1 and G2 | Read the code for red flags; build it with its parts. |
| 5 | `test` | G3 and G4, including the "tests that test nothing" catch | Run its tests, then check the tests are real; throw odd inputs at it. |
| 6 | `second_opinion` | G5 | A second, independent model tries to break it. |
| 7 | `trap` | G6 with decoy credentials | Put fake keys in the room and see who reaches for them. |
| 8 | `sign_off` | G7, the permission diff | A human reads exactly what changed and decides. The agent cannot approve. |
| 9 | `use` | `forge_run` in a fresh sandbox, the walls shown | It runs in a locked, windowless room with only what the label allows. |
| 10 | `reuse` | A second task finds the tool at once | Next time it is already on the shelf: no rebuild. |
| 11 | `caught` | A hostile tool reads a decoy and is quarantined | A tool that behaves in testing and misbehaves later is caught and pulled. |
| 12 | `your_turn` | Quick start commands, GitHub link | Try it yourself. |

## Data: real events, safely shown

`scripts/build_tour_data.py` converts one recording from `demo/recordings/` (P18: ledger events plus the replayed
transcript) into `site/data/tour.json`:

```json
{"schema": 1, "source": "recording" | "illustrative", "recorded_at": "..." | null, "toolforge_version": "..." | null,
 "story": {"task": "...", "tool": "..."},
 "steps": [{"id": "test", "gates": ["G3","G4"], "events": [{"t": 0.0, "type": "forge.gate.passed", "label": "G3 self-tests passed", "facts": {"tests": 6, "strength_check": "passed"}}]}]}
```

* **Allowlist, not blocklist.** Only these payload keys may reach the page: gate ids, statuses, counts, durations, truncated hashes (first
  12 hex characters), tool name and version, permission summaries. Source code, prompts, environment values and free text from
  a tool never do. A test feeds a recording containing hostile text and a fake secret and asserts neither survives.
* Event `label` text is generated from the event type by a fixed template, not copied from the payload.
* When no recording exists the page uses `tour_illustrative.json` and shows a visible badge, "Illustrated example, not a
  recording". With a recording the badge says "Recorded from a real run on `<date>`, ToolForge `<version>`".

## The experience

* A stage (inline SVG) shows the actors: agent, ToolForge server, pipeline stations, shelf (registry), locked room (sandbox). Each
  step highlights the relevant part and animates one small thing. Under `prefers-reduced-motion` nothing moves; steps still change.
* A caption area shows the **Simple** or **Technical** text. A two-state toggle switches them; the choice is kept per
  visitor (wrap storage in try/catch; fall back to Simple).
* A **live ledger** panel prints the step's real events as monospace lines (type, label, facts), so a technical reader sees
  what happened and a non-technical reader sees that something was recorded.
* Glossary terms in captions are buttons that reveal a short plain definition and the analogy. Terms: MCP, sandbox, manifest,
  schema fuzzing, egress, decoy credentials, ledger, hash chain, quarantine, operator, Blast Radius.
* Controls: Back, Next, Play/Pause (off by default, 5 s per step, pauses on hover and focus), a numbered progress list with
  `aria-current="step"`, left and right arrow keys, Home and End. Captions sit in an `aria-live="polite"` region; focus never jumps.
* Without JavaScript, the section shows the twelve steps as a static ordered list using the Simple captions.
* The final step reuses the quick-start commands from `site.json` and the GitHub button style from the header.

## Writing the captions

Follow `site-brief.md`: short sentences, the shop analogy, no hype. Every step needs both variants. Simple captions must not
contain a term from the jargon list in `tests/test_tour_captions.py` unless it is marked as a glossary term. Technical captions
name the gate, the file or module and the exact check, and link to the doc that defines it.

## Tests

* `test_tour_data.py`: the schema validates; step ids match `tour_captions.json`; build is deterministic; redaction test above;
  `source` is `illustrative` when no recording exists and the badge text matches the source.
* `test_tour_captions.py`: every step has non-empty `simple` and `technical`; average sentence length of Simple captions is at
  most 20 words; no unexplained jargon; every glossary reference resolves.
* `test_tour_static.py`: the no-JavaScript fallback list contains all twelve steps; reduced-motion rule exists in the CSS; no
  external URLs (reuse the P19 offline checks).
* `tests/browser/test_tour_browser.py` (marker `browser`): arrow keys move steps; the toggle swaps caption text; Play advances then
  pauses on focus; the live region updates; no console errors.

## Acceptance

```
python scripts/build_tour_data.py demo/recordings/<recording> && python scripts/build_site.py --release
pytest -q
```

Open the site, click the hero's tour button, step through all twelve steps in both modes with the keyboard only.

## Re-recording

`python demo/run_demo.py --replay demo/transcripts/ --record demo/recordings/<name>/` (flag added in P18) then rerun
`build_tour_data.py`. Recordings are small, sanitised and committed; large run folders stay under `runs/` and are ignored.

## Pitfalls

Never show tool-authored text on the page as HTML: build DOM nodes with `textContent`. Never present the illustrated example
as a recording. Do not invent events that the recording lacks to make a step look better; if a step has no real events, say
"no event recorded for this step". Keep each caption short enough to read in ten seconds.
