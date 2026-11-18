# Instructions for any coding agent working in this repository

You are building ToolForge one patch at a time. The person you work for applies each patch with `git am`, so a patch
must apply cleanly to the current tip of `main`, stand on its own, and leave the repository green.

Patch ids: `Pxx` are the main line. `Sxx` are optional stretch patches. A patch the person has dropped is marked `[-]` in
`PROGRESS.md`; never build one that is dropped.

## 0. Preflight

Before reading the guide, check the ground you stand on. If any check fails, stop and tell the person; do not start.

1. `git status` is clean and you are on the latest `main`.
2. The suite that existed before your patch passes: `pytest -m "not docker and not llm and not attacks"` (skip this if no
   code exists yet, which is the case before P01).
3. `git config core.autocrlf` is `false` or unset, and no text file contains CRLF line endings (`.gitattributes` enforces LF).
4. `python3 scripts/check_guide.py` prints "guide is consistent". If it does not, tell the person before building anything.

## 1. Find your patch

1. Read `PROGRESS.md`. If the person named a patch (for example "P05"), that is your patch. Otherwise it is the first
   row whose box is not ticked and whose dependencies are all ticked.
2. Read, in this order: `docs/guide/00-index.md`, `01-conventions.md`, `02-architecture.md`,
   `03-minilocker-integration.md`, then your patch file `docs/guide/patches/Pxx-*.md` (or `Sxx-*.md`), then the patch files of
   the patches it depends on (skim for interfaces you must call), then any other document your patch file names (for
   example `docs/guide/site-brief.md` for P19 and P20, `04-optional-resources.md` for P10 and P11).
3. Read `docs/guide/DEVIATIONS.md` if it exists, and the "Notes for the next patch" in `PROGRESS.md`.
4. Read the code that already exists for the modules your patch touches. The guide describes intent and interfaces;
   the repository is the truth for what is already built.

## 2. Do exactly one patch

* Implement only what your patch file lists under Scope. Do not pull work forward from later patches and do not
  refactor unrelated code. If you spot a problem elsewhere, write it under "Notes for the next patch" in `PROGRESS.md`.
* Follow every rule in `01-conventions.md`.
* Never invent the API of an external library, including MiniLocker and the `mcp` SDK. Open the pinned source or the
  installed package and read the real signatures before you call them. If reality differs from the guide, follow
  reality and record the difference in `docs/guide/DEVIATIONS.md` (create the file if it does not exist: one dated
  bullet per deviation, saying what the guide said, what is true, what you did).
* Write the tests listed in the patch file, plus any others the change needs. A patch without tests is incomplete.
* One source of truth per list. The MCP tool list (`META_TOOLS`), the gate list (`GATE_INFO`), the settings, the attack
  catalogue and the benchmark tasks are defined once in code. Documents and the site are generated from them or checked
  against them by a drift test. Never hand-copy such a list.
* If the patch changes behavior that a document describes, update the document in the same patch.
* Do not commit secrets, generated data, virtual environments, or `.patch` files.

### If the patch is too big

If you cannot finish a patch at the quality bar in one pass (P06 and P14 are the likely ones), say so **before** writing code:
propose a split into `Pxxa` and `Pxxb` (each standalone, green, with its own tests), add both rows to `PROGRESS.md` in the first
part, and deliver only `Pxxa`. Do not ship a half-finished patch, and do not tick a box for work that is not done.

## 3. Check your work before you hand it over

Run, and fix until clean:

```
ruff check . && ruff format --check .
pytest -m "not docker and not llm and not attacks"
```

If you touched anything under `docs/` or `PROGRESS.md`, also run `python3 scripts/check_guide.py`.

Each patch file has an "Acceptance" section with extra commands. Tests marked `docker` need a Docker daemon; run them if
you can. If you cannot, say so plainly in your reply and list exactly which tests and commands were not run. Never
claim a command passed unless you ran it.

Definition of done: acceptance commands run and pass (or are listed as not run), tests added, documents updated,
`PROGRESS.md` updated, no secrets, no CRLF, nothing outside the patch's scope, every limitation written down.

## 4. Produce the patch file

1. Update `PROGRESS.md`: tick your box and rewrite "Notes for the next patch" (decisions made, interfaces added, anything
   the next patch must know). Keep it short and factual.
2. Commit everything in one commit whose subject is `Pxx: <title from PROGRESS.md>` (`Sxx: ...` for stretch patches) and
   whose body lists the main changes and anything not verified.
3. Create the patch with `git format-patch -1 HEAD --stdout > NNNN-pxx-<slug>.patch`, where `NNNN` is the patch number
   padded to four digits (P05 becomes `0005-p05-execution-contract.patch`). Stretch patches use `01NN` (S01 becomes
   `0101-s01-bundles-and-attestations.patch`). Split parts add a letter after the number (`0006a-p06a-framework.patch`).
4. Check it: on a clean checkout of the previous tip, `git am --check NNNN-*.patch` must succeed.
5. Give the person the patch file and a reply containing: what the patch does in a few sentences; the exact commands to
   verify it; what you could not run; any deviation from the guide; and one line saying which patch is next.

If the person asks for a fix to an already applied patch, produce a new small patch named `fix-pxx-<slug>.patch` (for
example `fix-p05-timeout-mapping.patch`), commit subject `Fix Pxx: <summary>`, and add a line for it to `PROGRESS.md` under
"Notes for the next patch". Do not rewrite history.

## 5. Ground rules that matter more than speed

* Tools forged by agents are untrusted code. Anything that runs them, or reads what they produce, treats it as hostile
  input. Do not weaken a security check to make a test pass; fix the test or raise it in your notes.
* Be honest in code, docs, results and the website. If something is not measured, say "not measured". If a limitation
  exists, write it down in the docs rather than hiding it. A claim about something that is not built is a bug.
* Keep third-party code out of `src/`. MiniLocker is used only through the adapter described in
  `docs/guide/03-minilocker-integration.md`.
* The site under `site/` makes no external requests at build time or run time, shows no number that is not in a results
  file, and says "Not measured yet" where a result does not exist.
* Never put a real key, token or private data in the repository, in a test, in a ledger or in a prompt. Keys live in `.env`
  (git-ignored). If one is committed by mistake, say so immediately and tell the person to revoke it before anything else.
