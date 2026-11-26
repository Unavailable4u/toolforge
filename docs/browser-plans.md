# Browser plans (P14b)

A tool of `kind: "browser_plan"` operates a web page without any code of its own. The agent writes a **plan**, a list of steps from
a closed vocabulary, as data. ToolForge validates it, checks every `goto` host against the hosts an operator approved, runs it in
MiniLocker's browser sandbox and returns only the fields the plan extracts.

What an agent needs to write one (the manifest, the steps, templates, recorded pages and a complete example) is the `browser-plan`
recipe: `forge_recipe` with kind `browser-plan`, or `src/toolforge/adapters/recipes/browser-plan.md`. A reference pack is in
[`examples/toolpacks/browser_headlines`](../examples/toolpacks/browser_headlines/). Its host does not exist and its pages are made up.

## Setting it up (operator)

The browser image is MiniLocker's. It is built on your machine, once, from the submodule (it downloads Playwright and Chromium, so
it needs the network):

```text
git submodule update --init
pip install -e ".[sandbox]"
cd third_party/minilocker
docker build -f images/browser/Dockerfile -t minilocker-browser:latest .
```

`MINILOCKER_BROWSER_IMAGE` names another tag. `toolforge doctor` does not check this image. Without it:

* gate G6 of a plan is `skipped` ("the browser sandbox was not audited"), never `passed`, so an approval request shows that
  containment was not measured;
* calling an active plan fails with `sandbox_unavailable`. Nothing is recorded against the tool.

## What each gate does for a plan

| Gate | For a plan |
| --- | --- |
| G1 | The ordinary static checks, plus the plan: closed vocabulary, limits, literal approved hosts, no template in a selector, no credential-like selector or templated argument; only `plan.json`, `fixtures/*.json` and `.md` or `.txt` notes. |
| G2 | Freezes the package. There is nothing to install. |
| G3 | **Dry run.** Every example that has an `output` and is not `live` runs against the recorded pages; the extracted fields must equal the output exactly. A plan with no such example, or whose examples expect only empty fields, fails. Stored inputs that broke an earlier version are not replayed. |
| G4 | **Host and step limits.** Generated inputs and a fixed list of hostile values are put into every templated argument. Each result must be refused or keep every `goto` on an approved host, with the same steps and selectors. Nothing runs. |
| G5 | `skipped`: the challenger does not review plans. A plan is therefore never `verified`, at best `verified_basic`, then `pending_approval`. |
| G6 | **Browser sandbox audit.** A browser container is started with no network and its settings, as Docker reports them, must meet the same invariants as a code sandbox. The plan is not run and no decoy is planted. |

A skipped G6 is not repeated for the same version. After building the image, submit the plan again with a higher version.

## Approval

A plan declares at least one network host, so it always needs a human decision (`toolforge approvals`, see
[approvals.md](approvals.md)). `toolforge approvals show` lists every step of the plan, read from the frozen package. **Read them
before you approve**: approving the hosts approves every click and every piece of typed text the plan can send to them. Examples
marked `live` run once on the real site before the version becomes active.

## One run

1. The arguments are checked against `input_schema`, and the plan is rendered. A value that cannot be placed safely (a `.` or `..`
   path segment, an invisible character, too long) is `InvalidArguments`: nothing starts and the tool is not blamed.
2. The egress proxy starts with the plan's hosts and a browser container joins its network.
3. The steps run in order within `limits.timeout_s` (the setting default when unset). After each browser reply the page's address is
   read back; if it is not on one of the plan's hosts the run stops.
4. The fields are returned as `{"fields": {...}}`, cleaned like all tool output and fenced by the server. The container is destroyed.

| `error.type` | Meaning | Counted against the tool |
| --- | --- | --- |
| `InvalidArguments` | The arguments are wrong or cannot be put into the plan. Nothing ran. | no |
| `Crash` | A step failed, the page did not have the text the plan anchors on, or the page moved to a host that is not approved. | yes |
| `Timeout` | The run took longer than the limit. | yes |
| `OutputTooLarge` | The fields are larger than `limits.output_kb`. | as for any tool |
| `SandboxCrashed` | The browser container stopped working. | yes |

A tool that keeps failing is quarantined like any other ([repair.md](repair.md)), which is what happens when a site changes. Fix it
with a corrected plan: a new version, or `forge_repair` with your own `package`. `forge_repair` without a `package` asks the builder
model, whose instructions are to write a Python tool: nothing refuses that for a plan, and it has not been tried. Whatever it
returns goes through the same gates and waits for an operator, like any repair.

The ledger of a call holds `forge.call.started` (with `kind`, the number of steps and the browser image name),
`sandbox.created` for the browser container with its settings, one `egress.<decision>` event per request the proxy saw (with
`source: "browser"`), `sandbox.destroyed`, and `forge.call.finished` (with `steps_run`, `pages`, `egress_attempts`,
`egress_blocked`). Page text, field values and argument values never enter the ledger. `pages` counts the pages opened by `goto`.

## Limits you should know

* **Host control is coarse.** The egress proxy has one allowlist for all sandboxes of a process and passes **subdomains** of an allowed
  host. A page can make the browser load resources from such a host, including one approved for another tool. The plan itself stops
  when the page it is on leaves its own hosts, but only after the browser has already fetched that page.
* **Third-party resources are blocked** by the proxy unless their host is allowed. A page that needs them may not render. A Blast
  Radius report that samples such a call lists them as hosts the manifest does not declare and says `violation_seen`, even though
  the proxy stopped the connection. Many public pages load such resources.
* **Only HTTPS on port 443.**
* **The browser reads what it shows.** There is no reading by selector: only the visible text (cut at 12000 characters) and at most 40
  interactive elements (links shortened to 200 characters).
* **Pages change.** The dry run proves the plan against a recording. Only a real run shows the site still looks that way.
* **Pages may still be loading** after a click or a submit; a plan adds `wait_for`.
* **The credential check is a heuristic on names.** It cannot know what an element is. A plan has no secret to type, so what it could
  send is only what the caller passes in.
* **A plan acts on a real site.** A `click` or a `type` can change something there (a vote, a search log, a form), with no login.
* **The real-secret audit is not measured** for a browser call, and no decoy is planted in the browser container.
* **Bundles** ([PROVENANCE.md](PROVENANCE.md)): a plan can be exported and imported, and the receiver's gates judge it again. The
  attestation's runtime image digest is that of the code image, when there is one; the browser image is not recorded.
* **Not supported:** logins, sessions, files, downloads, screenshots as output, `toolforge run --dev-unverified`, direct tool
  exposure (`TOOLFORGE_EXPOSE_VERIFIED`), a model writing a plan (`forge_build`) or repairing one.

## Not run here

The tests in `tests/` drive the validator, gates, adapter and run path with recorded pages and a scripted browser.
`tests/docker/test_browser_plans.py` needs Docker, the browser image and the network, and says why it skips when Docker or the
image is missing. While this was built, **no real page was loaded through the egress proxy**, and MiniLocker's own browser image was
not built: the container audit, the proxy's block and the reference pack's gates ran with a stand-in image (`PROGRESS.md`).
