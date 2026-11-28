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
| `{{bench_table}}`, `{{bench_meta}}`, `{{context_table}}`, `{{demo_meta}}` | `site/data/results.json` |
| `{{limitations}}` | the bullets of `docs/KNOWN_LIMITATIONS.md` |
| `{{quickstart}}`, `{{capabilities_table}}`, `{{features_list}}` | `site/site.json` |
| `{{partial:NAME}}`, `{{svg:NAME}}`, `{{icon:NAME}}` | `site/src/partials/NAME.html`, `site/src/assets/NAME.svg`, `site/src/assets/gates/NAME.svg` |
| `{{repo_url}}`, `{{title}}`, `{{tagline}}`, `{{description}}`, `{{version}}`, `{{generated_on}}`, `{{site_url}}`, `{{social_meta}}` | the resolved address, `site/site.json`, the package version and `results.json` |

An unknown placeholder or partial stops the build. The output is deterministic: two builds are byte-identical.

## Editing the copy

Edit the files in `site/src/partials/`; they are plain HTML. The rules the tests hold you to:

* One sentence at the start of each section that someone could repeat to a friend, in a `<p class="summary">`.
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
stand-in sandbox, which gives no isolation, and the page says so.

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
pytest -m browser          # needs: pip install playwright && playwright install chromium
```

The browser tests load the built site over a local server in headless Chromium at several widths: no console errors, no failed or
cross-origin request, no sideways scroll, a working theme toggle, menu, copy button and keyboard path, reduced motion respected, and a page that
reads fully with JavaScript off. They are skipped when Playwright or its Chromium is missing.

## Not built yet

The interactive guided tour (P20). The `#tour` section holds a written summary and says the interactive version is planned.
