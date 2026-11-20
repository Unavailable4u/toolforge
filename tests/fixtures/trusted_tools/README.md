# Trusted tools

Packages written by us, checked in, and run through the real `harness.py` on the host by `tests/test_harness.py`. This is the only
place where a test may execute a tool outside a sandbox, and only code that lives in this folder (or in `examples/toolpacks`, see
`tests/test_run_path_local.py`) may be run that way: never a generated or submitted package. Each folder is a package root
(`tool.py` with `run(args)`), not a complete package: there is no manifest.
