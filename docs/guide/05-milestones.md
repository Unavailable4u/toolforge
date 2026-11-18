# Milestones and cut lines

Twenty-one patches (plus one optional) is a lot for one person. This page groups them into milestones that each end with something you can run
and show, and says honestly what can be dropped if time runs short.

Sizes are rough relative effort from reading the specs (S, M, L), not hours. Re-estimate after P04, which is the first patch
that touches Docker and MiniLocker for real.

| Patch | Size | Patch | Size | Patch | Size |
| --- | --- | --- | --- | --- | --- |
| P01 | S | P08 | M | P15 | M |
| P02 | M | P09 | L | P16 | L |
| P03 | M | P10 | M | P17 | M |
| P04 | L | P11 | M | P18 | M |
| P05 | M | P12 | M | P19 | M |
| P06 | L | P13 | M | P20 | M |
| P07 | M | P14 | L | P21 | M |

## Milestones

| M | Name | Patches | You can now |
| --- | --- | --- | --- |
| M0 | Foundations | P01, P02, P03, P04 | Validate a package, keep versions in a registry, open a hardened sandbox, run `toolforge doctor`. |
| M1 | It runs tools | P05 | `toolforge run --package ... --dev-unverified` runs a tool inside a sandbox and returns a result envelope. |
| M2 | Verified tools over MCP | P06, P07 | An MCP client (Claude Desktop, an IDE, your own agent) can `forge_submit`, you `toolforge promote` the result, the client can `forge_find` and `forge_run`. **First point worth screen-recording.** |
| M3 | Trustworthy | P08, P09, P10 | Decoy-credential containment, human approvals with a permission diff, an independent challenger. Tools reach `verified` and `active` on their own merits. |
| M4 | Builds and improves itself | P11, P12 | Say what you need in words and get a verified tool; failing tools are repaired, ranked, quarantined, rolled back. |
| M5 | Software and games | P13, P14 | Stateful sessions, the runtime image, recipes, reference packs for SQLite, formats, HTTP, a browser plan and games. |
| M6 | Proof | P15, P16, P17 | Attack Lab verdicts, benchmark numbers with intervals, and the operator dashboard. |
| M7 | Show it | P18, P19, P20, P21 | Demo and docs, the landing site and tour, the hardening pass and a tagged release. |
| S | Optional | S01 | Portable bundles and signed provenance. |

## Smallest version that still tells the whole story

If you must stop early, stop at a milestone boundary, never mid-milestone. The smallest convincing project is:

**M0 + M1 + M2 + M3 + P11, then a trimmed P15 and P18.**

That gives: forge, verify, contain, approve, build from words, an attack run that shows containment, and a demo. It leaves out
sessions, adapter kits, the benchmark, the dashboard and the polish.

## What can be dropped, and what that changes

| If you drop | Then | And you must |
| --- | --- | --- |
| P12 (repair, ranking, rollback) | Search stays at P03's basic ranking; no repair loop, no rollback (release and quarantine remain, they are P08). | Remove "reuse and repair" claims from every doc and the site; P17 loses its rollback endpoint and its dependency on P12. |
| P13 and P14 (sessions, adapters) | No games, no browser plans, no runtime image. | Drop the software-use tier from the README and site; P16 loses tier 2 and 3 tasks, keep the tier 1 tasks; P16's dependency on P14 becomes a dependency on P11 and P12 only; P17 loses its sessions tab and its dependency on P13. |
| P16 (benchmark) | No numbers about agent success. | Say "not measured" everywhere. Never imply results. |
| P17 (dashboard) | Operator works through the CLI only. | Remove the dashboard from docs and compose. |
| P20 (tour) | The site is still complete without it. | Keep the `#tour` anchor out of the nav. |

Dropping a patch means editing `PROGRESS.md` (mark it "dropped" with a one-line reason) and every document that mentions it.
A claim in a document about something that was not built is a bug (see `AGENTS.md`, ground rules).

## A working rhythm that suits a solo project

1. One patch at a time. Apply it, run its acceptance commands yourself, read the diff once.
2. Tick the box only after the acceptance commands pass on your machine.
3. After each milestone, spend ten minutes using the thing as a user. Write what annoyed you under "Notes for the next patch".
4. Re-read `docs/guide/DEVIATIONS.md` at each milestone; the guide is a plan, the code is the truth.
