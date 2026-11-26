# The runtime image (P14a)

Tool sandboxes run in one image: `python:3.12-slim` plus a fixed set of pinned libraries. It is built **on your machine** from
[`images/runtime/`](../images/runtime/) and tagged `toolforge-runtime:py312`, which is the default `TOOLFORGE_RUNTIME_IMAGE`. It is not on any
registry, so a sandbox cannot pull it and nothing is pushed.

```text
toolforge images build        # builds images/runtime/Dockerfile and prints the image id
toolforge doctor              # checks that Docker can see it
```

Building is the one step that needs the network (the base image and the wheels, once). Running a tool never does. `toolforge images build`
is a thin wrapper over `docker build`: it starts Docker with an argument list, checks the tag before it does, and returns Docker's exit
code. `--tag` names another tag, `--context` another folder with a `Dockerfile`, `--no-cache` rebuilds every layer. If
`TOOLFORGE_RUNTIME_IMAGE` names a different image it says so, because sandboxes would not use the one just built.

## What is in it

Installed with `pip install --no-deps --only-binary=:all:` from `images/runtime/requirements.txt`: exact versions, wheels only, no dependency
resolution, so what is in the image is exactly this list. A test (`tests/test_runtime_image_files.py`) checks that these tables match the file,
and `tests/docker/test_runtime_image.py` checks that a real build holds exactly these libraries.

| Library | Version | Use |
| --- | --- | --- |
| `duckdb` | 2.0.1 | in-process analytical SQL over CSV and JSON |
| `PyYAML` | 6.0.4 | `yaml.safe_load` for YAML |
| `python-dateutil` | 4.0.0 | flexible date parsing |
| `beautifulsoup4` | 4.15.0 | HTML and XML parsing |
| `lxml` | 6.1.3 | fast parser behind `beautifulsoup4`, and XPath |
| `pillow` | 13.0.0 | reading and converting images |
| `numpy` | 2.5.5 | arrays and numeric work |
| `gymnasium` | 1.4.0 | toy-text environments (FrozenLake, Taxi, Blackjack) |
| `chess` | 1.11.2 | chess rules, moves and FEN (`python-chess`) |
| `jsonschema` | 4.26.0 | checking JSON against a schema |
| `tabulate` | 0.10.0 | plain-text tables |

What those need to import:

| Library | Version |
| --- | --- |
| `attrs` | 26.1.0 |
| `cloudpickle` | 3.1.3 |
| `Farama-Notifications` | 0.0.6 |
| `jsonschema-specifications` | 2025.9.1 |
| `referencing` | 0.37.0 |
| `rpds-py` | 2026.9.1 |
| `soupsieve` | 4.1.3 |
| `typing_extensions` | 4.16.0 |
| `tzdata` | 2026.6 |

Tools use the standard library and these libraries without listing them in `requirements`. A `requirements` entry is for a library that is **not**
here; it is installed in the build gate (G2) with network access to `TOOLFORGE_BUILD_ALLOW` only.

## What the agent sees

* `forge_runtime_info` reports the Python version, the libraries and their versions as read from the image, the sandbox limits and the **image id**
  (`sha256:...`) Docker reports. If Docker cannot be read, each is `null` with a reason, never a guess.
* The same image id is written to the ledger: `forge.call.started` and `forge.session.opened` carry `image` and `image_id`, so a record of a call says
  which image it ran in. `null` means it was not read.
* `forge_recipe` serves recipes that use the image: `sql`, `file-format`, `http-api` and `game-state` (and `package-format`, which lists the libraries).

## Changing it

Edit `images/runtime/requirements.txt` (an exact pin, plus whatever the new library needs, the whole closure), update the tables above, run
`toolforge images build`, then `pytest -m docker`. The reference packs in `examples/toolpacks/` are the check that the image still runs them.

## Honest limits

* The base `python:3.12-slim` is pinned by tag, not by digest, so two builds on different days can differ in the base layer (the libraries do not).
  The image id in the ledger is what tells two builds apart.
* The image is built per machine. Two machines that built it at different times have different ids; there is no shared registry on purpose.
* Pillow, lxml and numpy parse untrusted bytes. The sandbox, not the library, is the boundary: no network, read-only root, no capabilities.
