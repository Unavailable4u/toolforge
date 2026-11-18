# Third-party code and authorship

* **ToolForge** (everything under `src/`, `tests/`, `bench/`, `demo/`, `examples/`, `docs/`) is original work in this
  repository.
* **MiniLocker** (https://github.com/shaikh-dotcom/MiniLocker, MIT) is an external dependency by the same author. It is
  consumed as a git submodule at `third_party/minilocker`, pinned to one commit, and imported only through
  `src/toolforge/sandbox/minilocker_runner.py`. No MiniLocker source file is copied, vendored or modified in this
  repository. Any change MiniLocker itself needs is made in the MiniLocker repository and then adopted by moving the pin.
* Python packages are listed with exact versions in `pyproject.toml` and keep their own licenses.
* **The website** (`site/`, P19 and P20) is authored in this repository: its text, CSS, JavaScript, logo, diagrams and gate icons are
  original work, drawn as inline SVG. It uses system font stacks only, loads nothing from a third party and contains no stock
  imagery. If the official GitHub mark is ever used instead of the generic icon, it comes from GitHub's own published asset under
  their logo guidelines and is recorded here. The social image is rendered from `docs/assets/cover.svg`.
* **`scripts/check_guide.py`** is a small standard-library checker for this guide, original to this repository.
