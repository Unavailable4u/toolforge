"""The tool table in `docs/clients.md` and in the README is `META_TOOLS`, and `docs/clients.md` mentions every setting it relies on."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from toolforge.config import Settings
from toolforge.server.tools import META_TOOLS, tools_markdown

ROOT = Path(__file__).resolve().parent.parent


def table_in(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    match = re.search(r"<!-- tools:begin -->\n(.*?)\n<!-- tools:end -->", text, re.DOTALL)
    assert match, f"{path.name} must wrap the tool table in tools:begin / tools:end markers"
    return match.group(1)


@pytest.mark.parametrize("name", ["docs/clients.md", "README.md"])
def test_the_tool_table_matches_META_TOOLS(name: str) -> None:
    assert table_in(ROOT / name) == tools_markdown(), (
        f"{name} is out of date: replace the text between the tools markers with toolforge.server.tools.tools_markdown()"
    )


def test_the_table_has_one_row_per_tool() -> None:
    rows = [line for line in tools_markdown().splitlines()[2:]]
    assert [re.match(r"\| `(\w+)`", row).group(1) for row in rows] == [m.name for m in META_TOOLS]


def test_every_tool_has_a_summary_and_a_description_written_for_a_model() -> None:
    for tool in META_TOOLS:
        assert tool.summary.endswith(".") and len(tool.description) > 150, tool.name
        expected = {
            "forge_build": "P11",
            "forge_repair": "P12",
            "forge_report_problem": "P12",
            "forge_session_close": "P13",
            "forge_session_rewind": "P13",
            "forge_session_list": "P13",
        }
        assert tool.since == expected.get(tool.name, "P07"), tool.name


def test_the_clients_guide_names_the_server_settings() -> None:
    text = (ROOT / "docs" / "clients.md").read_text(encoding="utf-8")
    for field in (
        "server_host",
        "server_port",
        "server_token",
        "server_allowed_hosts",
        "server_allowed_origins",
        "expose_direct",
    ):
        assert field in Settings.model_fields
        assert f"TOOLFORGE_{field.upper()}" in text, field
