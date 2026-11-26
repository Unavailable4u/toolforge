# P14b: Browser plans and the GUI kit

Depends on: P14a.

## Goal

Let an agent forge a tool that operates a web page, safely: a **plan** (data, not code) that the host validates and runs in MiniLocker's
browser sandbox. Document tier 4 (desktop GUIs) without claiming it.

## Scope

`src/toolforge/adapters/browser_plan.py` (the plan vocabulary and its validator), `adapters/browser_fixture.py`,
`sandbox/browser_adapter.py` (an allowed MiniLocker importer), the `browser_plan` package kind and the `permissions.browser` manifest field
(the manifest rejects it as reserved until this patch), plan gates G1, G3, G4 and G6, the `browser-plan` recipe (added to `RECIPE_KINDS`), the
`browser_headlines` reference pack, the golden test for it, the plan tests, and `docs/gui-kit.md` (documentation only).

## Browser plans (kind `browser_plan`)


A forged web tool is **data, not code**: a JSON plan the host executes against MiniLocker's `BrowserSandbox`
(`sandbox/browser_adapter.py`, a new allowed importer; read `browser_worker.py` for the supported actions first). The manifest
has `kind: "browser_plan"` and `permissions.browser: true` plus the hosts it may open; the plan is a list of steps from a closed
vocabulary (`goto`, `click`, `type`, `extract_text`, `extract_attr`, `wait_for`, `screenshot`), with `{{arg}}` templating from
the tool input and `extract_*` steps naming output fields. The host validates the plan against the DSL, checks every
`goto` host against the approved list, runs it, and returns the extracted fields through the standard fencing (page text is
untrusted; use `textsafety` exactly as MiniLocker does). Verification for this kind: plan validity (G1), a dry run against a
fixture page served inside the test suite (G3), host and step limits (G4), the browser sandbox audit (G6), approval for hosts (G7).
Keep the vocabulary small; reject anything not in it.

## Reference pack

`examples/toolpacks/browser_headlines` (a plan with recorded fixtures), added to `tests/docker/test_golden_packs.py`.

## Tier 4 (optional, experimental)


Write `docs/gui-kit.md` describing an `toolforge-runtime-gui` image (virtual display, screenshot and input tools), why it
needs a vision step, what could be built without one (apps exposing text state), and its risks. Implement only if the rest is
solid. If implemented: separate image, separate recipe, clearly labelled experimental, and no claim in the README beyond the doc.

## Tests

Plan DSL validator unit tests (unknown step, host not approved, template injection attempts, credential selectors); the executor against
a scripted browser and against recorded fixtures; the hostile-value battery for every templated argument; the browser sandbox audit with a
scripted sandbox; the `browser-plan` recipe's example through the gates.

## Acceptance

`pytest -m docker tests/docker/test_golden_packs.py -q`, `pytest -q`.

## Pitfalls

Browser plans never carry credentials; sites that need login are out of scope in v1. The browser worker cannot read an element by
selector, only the page's visible text and a short list of interactive elements, so the vocabulary is built on that; record the
consequence in `docs/guide/DEVIATIONS.md`.
