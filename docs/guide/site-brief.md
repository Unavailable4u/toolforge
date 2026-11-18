# Site brief

Read this before touching `site/` (P19, P20). It holds the audience, the writing rules, the page map and the design
direction. The engineering (build script, tests, deployment) is in `patches/P19-landing-site.md`; the tour is in
`patches/P20-guided-tour.md`.

## What the site is

The README in a form people actually read. A stranger should understand what ToolForge is within 30 seconds, and a developer
should be able to judge it without opening the repository. It explains what was built, how it works, the architecture, the
capabilities and features, how it was verified, and where it stops. Top right of every page state: a **GitHub** button that
opens the repository.

## Three visitors, three clocks

| Visitor | Time they give | What they must leave knowing |
| --- | --- | --- |
| Curious, non-technical | 30 seconds | What it is, why it matters, and that safety is designed in. The hero and the tour button do this. |
| Developer or reviewer | 3 minutes | How it works, what is real, how it is checked. Tour, architecture, gates, tool list. |
| Someone who will run it | 15 minutes | Whether to trust it and how to try it. Quick start, security model, limits, evidence, how it was built. |

## Plain-language rules

1. Open with the problem in ordinary words, then the idea, then the mechanism. Never open with a stack of acronyms.
2. Every section starts with one sentence a non-technical reader could repeat to a friend.
3. Any technical term is explained in the sentence where it first appears, or is a glossary term (P20 ships `glossary.json`).
4. Short sentences. In the "simple" copy aim for an average under 20 words and no hype words ("revolutionary",
   "enterprise-grade", "military-grade", "seamless").
5. One running analogy, used consistently: **a tool shop with an inspector at the door.** Nothing goes on the shelf until it has
   been inspected, tested and tried in a locked room, and every inspection is written in a notebook that cannot be quietly edited.

| Technical term | In the analogy |
| --- | --- |
| tool package | a new tool brought to the shop |
| manifest | the label stating what the tool may touch |
| gates G1 to G7 | inspection stations |
| sandbox | a locked, windowless test room |
| decoy credentials | fake keys left lying around to see who reaches for them |
| ledger | a tamper-evident notebook |
| registry | the shelf of approved tools |
| operator | the person who holds the keys and signs off |
| Blast Radius report | the inspector's report on what a tool could have reached |
| MCP | the standard plug that lets any AI assistant use the shop |

6. **Honesty rules.** Every number on the page comes from a results file and shows its date, run count and model, or it says
   **"Not measured yet"** in the hatched style described below. Every capability carries a status chip: `built`,
   `experimental` or `planned`. The limits section is as prominent as the features. A claim without evidence is deleted.
7. No stock imagery, no invented logos, quotes or testimonials, no "trusted by", no live star counts or other third-party calls.

## Page map

Anchors are stable (they are linked from the README and from the tour). Each section lists where its facts must come from.

| Anchor | Section | Must say | Facts come from |
| --- | --- | --- | --- |
| `#top` | Header and hero | One-sentence description; two buttons ("Take the 2-minute tour", "Read the quick start"); a small animated strip of the lifecycle (forge, verify, run, reuse). | `site.json` tagline, README |
| `#problem` | The problem | Agents get stuck when no tool exists. Today a human writes it, or the agent runs unrecorded code. Why both are bad. | `00-index.md` |
| `#idea` | The idea in four steps | Forge, Verify, Run, Reuse in the shop analogy, each with one concrete example. | `00-index.md`, README |
| `#tour` | Guided tour | The interactive walkthrough (P20). Until P20 exists: a static three-step summary. | P20 |
| `#architecture` | How it fits together | The diagram (inline SVG), the five components, and the trust zones in plain words ("who is trusted and who is not"). | `docs/ARCHITECTURE.md` |
| `#gates` | The seven gates | One card per gate: what it checks, what it catches, a one-line example. | `GATE_INFO` constant (P06) |
| `#capabilities` | What you can forge | The tier table (data files, HTTP APIs, web pages, games, desktop GUIs) with honest status chips. | P14 tiers |
| `#features` | Features | Reuse and ranking, repair, rollback and quarantine, sessions and rewind, approvals with a permission diff, Blast Radius reports, dashboard, Attack Lab, benchmark, works with any MCP client. Each chip-marked. | README, PROGRESS |
| `#security` | Security model | What the container and gates guarantee, and the explicit non-guarantees. Plain words first, details second. | `SECURITY.md` (P21), `KNOWN_LIMITATIONS.md` |
| `#proof` | Evidence | Attack Lab table (verdict per attack), benchmark table with intervals, a sample Blast Radius report, how to re-verify a ledger. | `site/data/results.json` |
| `#tools` | MCP tool reference | The generated table of meta-tools. | `META_TOOLS` constant (P07) |
| `#quickstart` | Quick start | Install, `doctor`, serve, connect a client, promote, run. Copy buttons. States Docker is required for real sandboxes. | README, `docs/clients.md` |
| `#built` | How it was built | Built patch by patch with an AI coding agent from a written guide; each patch one reviewed commit. Credit to MiniLocker. Link to `docs/guide/`. | `NOTICE.md`, `PROGRESS.md` |
| `#limits` | Limits and what is next | The honest list. | `docs/KNOWN_LIMITATIONS.md` |
| `#faq` | FAQ | See below. | all of the above |
| footer | Footer | Licence, version, MiniLocker credit, GitHub link, "generated on" date. | `site.json`, `__version__` |

### FAQ (minimum set)

Is it safe to run? Do I need an API key? Which AI assistants work with it? How is this different from letting an agent run
code? What can it not do? Who is it for? Why Python only? Where does my data go? Can an agent approve its own tool? (No: there
is no MCP tool that approves.)

## Header and the GitHub button

* Left: wordmark "ToolForge" (text plus a small inline SVG mark drawn for this project; no borrowed logos).
* Centre: anchor navigation (Idea, Tour, Architecture, Gates, Features, Security, Evidence, Quick start). Below 800 px it
  collapses into a menu button that works without a mouse.
* **Right: the GitHub button.** An ordinary `<a>` with the label "GitHub" and a generic brackets-and-arrow icon, href from
  `site.json:repo_url`, `target="_blank"`, `rel="noopener noreferrer"`, visible focus ring, at least 44 px tall on touch
  screens. Do not hand-draw the GitHub mark. If you want the official mark, use GitHub's own published asset under their
  logo guidelines and record that in `NOTICE.md`.
* The header is sticky and never covers the target of an anchor jump (`scroll-margin-top`). A "skip to content" link is the
  first focusable element.

## Visual direction

Concept: **a workshop with an inspector.** Warm, precise, a little industrial. Not a purple-gradient AI landing page.

* Colour: ink `#14161a`, paper `#f6f3ee`, ember accent `#ff6a2b`, steel `#8a94a3`, pass `#2f9e6f`, fail `#d64545`. Light and
  dark themes via `prefers-color-scheme` with a manual toggle. Text contrast at least 4.5:1 in both themes.
* **"Not measured" is its own visual state:** a grey diagonal hatch with the words "Not measured yet". It must never reuse the
  green pass colour or a tick. This mirrors the repository rule that a missing measurement is never zero or green.
* Type: system font stacks only (no web fonts, no external requests). Heavy tight sans for headings, a monospace stack for
  code and ledger lines. A clear scale (for example 14, 16, 20, 28, 40, 64 px) and a 70-character reading measure.
* Layout: one column of content with generous space, diagrams allowed to break out wider. Cards have one border style, not three.
* Motion: small and purposeful (the lifecycle strip, gate highlights, the tour stage). All of it stops under
  `prefers-reduced-motion`, and nothing autoplays with sound.
* Diagrams are inline SVG authored for this project, themed with CSS variables, each with a text alternative.
* Responsive from 360 px wide up. No horizontal scrolling except inside code blocks and wide tables, which scroll on their own.

## Technical constraints (tested in P19)

Static files only. No framework, no bundler, no Node. No external requests of any kind: no CDN, fonts, analytics, trackers,
embeds or star-count fetches (a `Content-Security-Policy` meta tag enforces `default-src 'self'`). No cookies. Scripts live in
files, not inline. Works with JavaScript disabled for all reading content; only the tour, theme toggle and copy buttons need it.
Total shipped size under 400 KB excluding the social image.

## Sharing metadata

`<title>`, description, Open Graph and Twitter-card tags, a theme-colour, an inline SVG favicon, and one social image rendered
from `docs/assets/cover.svg` (P18).
