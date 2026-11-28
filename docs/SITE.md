# The website

A static, single-page site that explains ToolForge to someone who has never heard of it. It is built from `site/src` into `site/dist` by
`scripts/build_site.py`, with no Node and no bundler, and published to GitHub Pages by `.github/workflows/pages.yml`. The page makes no
network request, sets no cookie and runs no analytics.

## Build and preview

```
pip install -e .
python scripts/build_site.py                      # a preview build into site/dist
python -m http.server -d site/dist 8000           # then open http://localhost:8000 (not file://)
python scripts/build_site.py --release            # what the workflow runs
```

The build prints where it got the repository address: `--repo-url`, then `repo_url` in `site/site.json`, then `GITHUB_REPOSITORY`, then the
`origin` remote (an ssh remote is turned into an https address; a token in a remote is dropped). With none of them it uses a placeholder and
warns. `--release` refuses a placeholder, so a dead GitHub button cannot be published. No address is ever typed into a script or a page.

## Where each part comes from

The page is `site/src/index.html` plus one file per section in `site/src/partials/`. A `{{placeholder}}` is filled from the one place that
already knows the answer, so a list is never copied by hand.

| Placeholder | Source |
| --- | --- |
| `{{tools_table}}` | `META_TOOLS` in `toolforge/server/tools.py` |
| `{{gates_cards}}` | `GATE_INFO` in `toolforge/verify/pipeline.py` and the icons in `site/src/assets/gates/` |
| `{{settings_table}}` | the `Settings` fields, described by the comments in `.env.example` |
| `{{attacks_table}}`, `{{attacks_meta}}` | `ATTACKS` in `toolforge/attacks/catalog.py`, joined with `site/data/results.json` |
| `{{bench_table}}`, `{{bench_meta}}`, `{{context_table}}` | `site/data/results.json` |
| `{{tour_intro}}` | the badge, story and caveats in `site/data/tour.json` |
| `{{tour_steps}}`, `{{tour_progress}}`, `{{tour_glossary}}`, `{{tour_first_title}}`, `{{tour_first_caption}}`, `{{tour_first_note}}`, `{{tour_first_doc}}` | `site/src/tour_captions.json`, `site/src/glossary.json` and the notes in `site/data/tour.json` |
| `{{tour_quickstart}}` | the same `quickstart` list in `site/site.json` as `{{quickstart}}` |
| `{{limitations}}` | the bullets of `docs/KNOWN_LIMITATIONS.md` |
| `{{quickstart}}`, `{{capabilities_table}}`, `{{features_list}}` | `site/site.json` |
| `{{partial:NAME}}`, `{{svg:NAME}}`, `{{icon:NAME}}` | `site/src/partials/NAME.html`, `site/src/assets/NAME.svg`, `site/src/assets/gates/NAME.svg` |
| `{{repo_url}}`, `{{title}}`, `{{tagline}}`, `{{description}}`, `{{version}}`, `{{generated_on}}`, `{{site_url}}`, `{{social_meta}}` | the resolved address, `site/site.json`, the package version and `results.json` |

An unknown placeholder or partial stops the build. The output is deterministic: two builds are byte-identical.

## Editing the copy

Edit the files in `site/src/partials/`; they are plain HTML. The rules the tests hold you to:

* One sentence at the start of each section that someone could repeat to a friend, in a `<p class="summary">`.
* The tour's words are not in the partial: see [Tour](#tour).
* No inline `<style>`, `style=` attribute, inline `<script>` or event handler: the page's Content-Security-Policy is `default-src 'self'`
  and the browser tests run under it.
* Never write a measured number into HTML. Numbers come from `results.json` through the generated sections, wrapped in `<span class="fig">`.
* A capability or feature in `site/site.json` carries a status chip. `built` and `experimental` need every patch it names to be ticked in
  `PROGRESS.md`; the build checks this. `planned` is for what does not exist.
* Each quick start command in `site/site.json` must appear in the document it names (`README.md` or `docs/clients.md`); a test checks it.

## Evidence: refreshing the numbers

`site/data/results.json` is a committed snapshot written by `scripts/build_site_data.py`. It reads the newest of each of these, and
anything missing becomes `null`, which the page draws as the hatched "Not measured yet", never as zero:

```
toolforge attacks run --json                              # runs/attacks/attacks-<time>.json   (needs Docker)
python -m bench ...                                       # runs/bench/<time>/summary.json      (needs Docker and a model)
python -m bench context --out runs/bench/context          # context figures: no Docker, no model
python scripts/build_site_data.py                         # writes site/data/results.json
```

The committed snapshot holds no attack or benchmark result, because none is committed to the repository. It carries only the metadata of the
recorded demo (`demo/recordings/main/`), which the script validates with `demo.recording.validate` first. That recording was made with the
stand-in sandbox, which gives no isolation, and the [tour](#tour) says so next to its badge.

## Tour

The `#tour` section walks one tool through twelve steps: asked for, searched for, forged, checked by G1 to G7, used, reused, and in the last act
caught. It plays the events of one recorded demo run.

| File | What it holds |
| --- | --- |
| `site/data/tour.json` | generated and committed: the badge, the caveats and, per step, the allowed events |
| `scripts/build_tour_data.py` | turns a recording into that file (standard library only) |
| `site/src/tour_captions.json` | per step: `title`, `simple`, `technical`, the glossary `terms` it uses and the `doc` it links to |
| `site/src/glossary.json` | term id to `term`, `plain` and `analogy` |
| `site/src/tour_illustrative.json` | the sample story used only when there is no recording |
| `site/src/partials/tour.html`, `tour.js`, `tour.css`, `assets/tour-stage.svg` | the page itself |

### Re-record and rebuild

On a machine with Docker, so that the recording holds the real G6 decoy run:

```
toolforge images build
python demo/run_demo.py --replay demo/transcripts/ --record demo/recordings/main/
python scripts/build_tour_data.py                 # writes site/data/tour.json from demo/recordings/main
python scripts/build_site_data.py                 # the recording's metadata in results.json
pytest tests/test_demo_recording.py tests/test_tour_data.py tests/test_site_data.py
python scripts/build_site.py --release
```

Commit the recording, `site/data/tour.json` and `site/data/results.json` together: a test fails when `tour.json` is not what the committed
recording builds. A recording kept under another name is built with `python scripts/build_tour_data.py demo/recordings/<name>`; the drift test
reads `demo/recordings/main/`. Large run folders stay under `runs/` and are not committed.

Then read the tour. The captions say what the software does; each step's note, under the caption, says what this recording shows of it.
After a recording made with Docker, update the sentences that describe the old one by hand: the end of
[What the page says about its source](#what-the-page-says-about-its-source), the end of the Evidence section above, and the tour's line in
`docs/KNOWN_LIMITATIONS.md`.

### What may reach the page

A recording is treated as untrusted input. The script copies only the keys listed in `FACTS`, `METRICS` and `PERMISSIONS`: gate ids, statuses
and other words from closed lists, counts, durations, 12-character hash prefixes and tool names with a version. No open text is copied, so
source code, prompts, environment values and a tool's own words have no field to travel in. Event labels come from fixed templates. When the
repository's code can be imported the script also runs `demo.recording.validate` and refuses a recording that fails it; without it, it says on
stderr that only the allowlist was applied.

A tool's name is chosen by whoever wrote the tool. It is shown only when it has the shape of a manifest name (3 to 41 lower-case letters,
digits and underscores, starting with a letter, with an optional version), and the page writes it as text, never as HTML.

### What the page says about its source

* With a recording: "Recorded from a real run on `<date>`, ToolForge `<version>`". A recording made with the stand-in sandbox, or with scripted
  agent replies, adds a sentence saying so beside the badge.
* Without one (`site/data/tour.json` missing, or `--illustrative`): "Illustrated example, not a recording", sample events with no times and no
  figures. Naming a directory that does not exist is an error, so a typing mistake cannot publish the sample.
* A step without events says "No event recorded for this step." Skipped and failed gates are reported as such. Nothing is filled in.

The committed `demo/recordings/main/` was made with the stand-in sandbox and a scripted agent. In it G5 and G6 are skipped, and the hostile
tool is rejected at G1 before any decoy is read, so the `second_opinion`, `trap` and `caught` steps show less than the captions describe, and
their notes say which part is missing.

### Editing captions

In a caption `[[term_id]]` or `[[term_id|words shown]]` makes a glossary button and backticks make code. `tests/test_tour_captions.py` holds
both variants to: at most 45 words, Simple sentences of at most 20 words on average, no jargon in Simple text unless it is a marked term, every
reference resolving, and file and tool names that exist.

### Controls

Back, Next and a numbered list of steps; left and right arrow keys, Home and End while focus is inside the tour; a Simple and Technical toggle
(the choice is kept in the browser's local storage, the only thing the site stores for the tour); Play, off by default, 5 seconds a step, which
waits while the pointer or focus is on the text, the events or the step list. Without JavaScript, or when a data file does not load, the twelve
steps are a written list.

## The social preview image

The build renders `docs/assets/cover.svg` to `assets/social.png` with the converter `scripts/render_cover.py` finds. With none installed it
warns, leaves the image out and uses a plain share card; the site is otherwise identical. The workflow installs `librsvg2-bin` for this.

## Deploying

Once, in the repository's **Settings, Pages, Source**, choose **GitHub Actions**. After that a push to `main` that touches the site, the
docs, the code its lists come from or the build scripts builds and deploys it. The site address is `https://<owner>.github.io/<repo>/`; every
link in the page is relative so that the sub-path works. Each action in the workflow is pinned to a full commit SHA with its tag in a comment.

## Tests

```
pytest tests/test_site_build.py tests/test_site_html.py tests/test_site_offline.py tests/test_site_data.py
pytest tests/test_tour_data.py tests/test_tour_captions.py tests/test_tour_static.py
pytest -m browser          # needs: pip install playwright && playwright install chromium
```

The browser tests load the built site over a local server in headless Chromium at several widths: no console errors, no failed or
cross-origin request, no sideways scroll, a working theme toggle, menu, copy button and keyboard path, reduced motion respected, and a page that
reads fully with JavaScript off. They are skipped when Playwright or its Chromium is missing.
