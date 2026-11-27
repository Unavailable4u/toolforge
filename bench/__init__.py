"""The ToolForge benchmark (P16): does an agent finish a task that needs a tool it does not have, with and without ToolForge?

`python -m bench` runs the task set (`bench/tasks/`) under three configurations and writes raw run files, `summary.json` and
`report.md`; `python -m bench context` measures what the server's responses cost in a model's context. The method, the hypotheses and
what the numbers can and cannot show are in `docs/BENCHMARK.md`.

This package is not part of the `toolforge` distribution: it is run from a checkout. It may import `toolforge`, never `tests/`.
"""
