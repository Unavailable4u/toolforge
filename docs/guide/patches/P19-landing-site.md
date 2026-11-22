# P19: Landing site

Depends on: P18.

## Goal

A public, static website that explains ToolForge plainly enough for someone who has never heard of MCP, and completely enough
that a developer can judge it without opening the repository. Top right: a GitHub button to the repository. The interactive tour
is P20; this patch leaves its section in place with a static summary.

Read `docs/guide/site-brief.md` first. It holds the audience, the plain-language and honesty rules, the page map and the
design direction. This file holds the engineering.

## Why it comes after the demo

The site shows measured numbers and real behaviour. It needs the final README and docs (P18), attack results (P15), benchmark
results (P16) and the recorded demo (P18). Content that is not yet true is shown with a `planned` chip, never as done.

## Scope

```
site/
  site.json                 repo_url, tagline, title, allowed_external, quickstart commands, og_image
  src/index.html            page skeleton with {{placeholders}}
  src/partials/*.html       one file per section (anchors listed in site-brief.md)
  src/styles.css  src/app.js
  src/assets/               logo.svg, architecture.svg, gate icons (authored here; no stock or borrowed imagery)
  data/results.json         committed snapshot written by scripts/build_site_data.py
scripts/build_site.py       stdlib only
scripts/build_site_data.py  stdlib only
.github/workflows/pages.yml
docs/SITE.md                how to build, preview, edit copy, deploy
tests/test_site_build.py  tests/test_site_html.py  tests/test_site_offline.py  tests/test_site_data.py  tests/browser/test_site_browser.py
```

Also: add a "Website" line to `README.md`, register the `browser` pytest marker (skipped when Playwright is absent), and
add `site/dist/` to `.gitignore` if not already there.

## Build

No Node, no bundler. `python scripts/build_site.py [--release]` copies `site/src` to `site/dist`, fills placeholders and writes
the result. Output is deterministic (sorted iteration, no timestamps except the `generated_at` taken from `results.json`).

Placeholders and where they come from (the site must not hand-copy what code already knows):

| Placeholder | Source |
| --- | --- |
| `{{repo_url}}`, `{{title}}`, `{{tagline}}` | `site/site.json` |
| `{{version}}` | `toolforge.__version__` |
| `{{tools_table}}` | `META_TOOLS` in `toolforge/server/tools.py` (P07) |
| `{{gates_cards}}` | `GATE_INFO` in `toolforge/verify/pipeline.py` (P06) |
| `{{settings_table}}` | the `Settings` model fields and `.env.example` |
| `{{attacks_table}}` | `toolforge.attacks.catalog.CATALOG` (P15) joined with the latest result in `results.json` |
| `{{bench_table}}`, `{{bench_meta}}` | `results.json` |
| `{{limitations}}` | bullet list parsed from `docs/KNOWN_LIMITATIONS.md` |
| `{{quickstart}}` | `site.json:quickstart` (a test asserts each command also appears in the README) |

**The repository address is never typed by hand into a script.** It is resolved in this order: `--repo-url`; a non-empty
`site/site.json:repo_url`; the `GITHUB_REPOSITORY` environment variable (set automatically inside GitHub Actions); then
`git remote get-url origin`. An ssh or https remote is normalised to `https://github.com/<owner>/<name>` (no `.git`, no trailing
slash) and anything that is not a GitHub address is an error. If nothing resolves, a placeholder containing `OWNER` is used, which
is fine for a local preview (the build prints a warning) and is refused by `--release`. A dead GitHub button must not ship.
The build prints where the address came from.

### `scripts/build_site_data.py`

Reads the newest `runs/attacks/*.json` (P15), `runs/bench/*/summary.json` (P16) and `demo/recordings/` metadata (P18) and
writes `site/data/results.json`:

```json
{"generated_at": "2026-..", "attacks": {"run_id": "...", "contained": 17, "total": 18, "per_attack": {"decoy_read": "contained"}} | null,
 "bench": {"run_id": "...", "model": "...", "runs": 5, "configs": {"baseline": {...}, "forge_cold": {...}, "forge_warm": {...}}} | null,
 "context": {"method": "estimate" | "measured", "date": "...", "sizes": {"10": {"meta": {...}, "direct": {...}}}} | null,
 "demo": {"recorded_at": "...", "toolforge_version": "..."} | null}
```

Any missing source is `null`, and the page renders the hatched **"Not measured yet"** state for it. Never substitute zero.
Benchmark cells show the Wilson interval beside every rate and the number of runs; the page says in one sentence that small
samples have wide intervals.

## The GitHub button

Exactly as in `site-brief.md`: a plain anchor, top right of the sticky header, `href` from `site.json:repo_url`, opens in a new
tab with `rel="noopener noreferrer"`, accessible name "ToolForge on GitHub", visible focus state. It is also repeated once
in the footer. No third-party widget, no star count.

## Deployment

`.github/workflows/pages.yml`: on push to `main` touching `site/**`, `docs/**`, `scripts/build_site*.py` or `src/**`, run
`python scripts/build_site.py --release`, upload `site/dist` as the Pages artifact and deploy. Permissions: `contents: read`,
`pages: write`, `id-token: write`. Pin each action by full commit SHA with the tag in a comment (look up current versions;
do not guess). Tell the person the one manual step: repository **Settings, Pages, Source: GitHub Actions**. The site URL is
`https://<owner>.github.io/<repo>/`; use relative links everywhere so a project sub-path works.

## Tests

`tests/test_site_build.py`: the build succeeds; two builds are byte-identical; no `{{` remains; `--release` fails on a
placeholder `repo_url`; the address resolution order and the ssh-to-https normalisation (table-driven, with a fake git remote); building with no `results.json` values still succeeds and shows "Not measured yet".

`tests/test_site_html.py` (stdlib `html.parser`): `lang` set; one `h1`; heading levels never skip; ids unique; every
`#anchor` link has a target and every anchor from the page map exists; every `img` has `alt`; the first focusable element is
the skip link; the header contains exactly one GitHub link with the right href, `target` and `rel`; meta description,
Open Graph tags and viewport present.

`tests/test_site_offline.py`: no `http://` or `https://` URL in any shipped HTML, CSS or JS except those in
`site.json:allowed_external` (the repo URL and a short list of documentation links); no cross-origin `src`; the CSP meta tag
is present with `default-src 'self'`; no inline `<script>` and no inline event handlers; `prefers-reduced-motion` appears in the
CSS; total `dist` size under 400 KB excluding the social image.

`tests/test_site_data.py`: tool table equals `META_TOOLS`; gate cards equal `GATE_INFO`; attack table ids equal the catalogue;
with `null` results no percentage appears in the evidence section; with sample results every figure on the page appears in
`results.json`. The context-cost figures (P16) appear only when `context` is not `null`, each with its `method` word (`estimate` or
`measured`) next to it and the sentence that they come from synthetic tools; no figure about another project appears anywhere.

`tests/browser/test_site_browser.py` (marker `browser`, skipped without Playwright): loads `dist` over a local static server at
375 px and 1280 px; no console errors; no failed requests; no horizontal scroll at 375 px; the GitHub button is visible at both
widths and reachable by keyboard; the theme toggle changes `data-theme`.

## Acceptance

```
python scripts/build_site.py --release && python -m http.server -d site/dist 8000
pytest -q
```

Then open `http://localhost:8000` (not `file://`, which blocks the same-origin data requests) and read it once top to bottom at
375, 768 and 1280 px. Every section in the page map exists; every number has a date or says "Not measured yet".

## Out of scope

The interactive tour (P20). Analytics, forms, comments, a blog, a custom domain.

## Pitfalls

Do not fetch anything from the network at build time or run time. Do not copy a table by hand when a constant exists. Do not
write a number into HTML. Do not claim a capability whose chip is not `built` or `experimental` in `PROGRESS.md`. Do not use
copy that sounds like marketing; the brief's honesty rules are tests of taste, apply them.
