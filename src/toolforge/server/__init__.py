"""The MCP server (P07): `build_server` in `app.py`, the tool table and handlers in `tools.py`, text safety in `fences.py`.

**Nothing in this package may import `toolforge.services.operator_actions`** (promote, and later release, rollback, approvals).
Agents reach ToolForge only through the tools in `tools.META_TOOLS`; an operator acts only through the command line or the
dashboard. `tests/test_server_tools.py::test_server_never_imports_operator_actions` enforces it.
"""
