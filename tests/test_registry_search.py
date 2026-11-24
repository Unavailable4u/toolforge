"""Search: finds active tools by their words, ignores everything else, survives hostile queries, and has a fallback."""

from __future__ import annotations

import logging
from collections.abc import Callable

import pytest

from toolforge.models.package import ToolPackage
from toolforge.registry import Registry, VersionRecord, search
from toolforge.registry.search import MAX_QUERY_TOKENS, fts_expression, query_tokens

Make = Callable[..., ToolPackage]
Activate = Callable[[VersionRecord], VersionRecord]

CSV_EXAMPLES = [
    {
        "input": {"path": "inputs/data.csv"},
        "files": {"inputs/data.csv": "city,population\nDhaka,22000000\n"},
        "output": {"rows": 1},
    }
]


@pytest.fixture
def stocked(registry: Registry, make_package: Make, activate: Activate) -> Registry:
    """Three active tools about different things."""
    activate(registry.add_version(make_package("word_count", "1.0.0"), created_by="agent"))
    activate(
        registry.add_version(
            make_package(
                "csv_summary",
                "1.0.0",
                description="Summarise a spreadsheet: column names, row counts and basic statistics for each column.",
                when_to_use="The task hands over tabular data and wants an overview of it.",
                examples=CSV_EXAMPLES,
            ),
            created_by="agent",
        )
    )
    activate(
        registry.add_version(
            make_package(
                "pdf_text", "1.0.0", description="Extract the plain text of every page of a PDF document."
            ),
            created_by="agent",
        )
    )
    return registry


def _names(registry: Registry, query: str, **kwargs: int) -> list[str]:
    return [hit.name for hit in registry.search(query, **kwargs)]


# --- Finding tools ---------------------------------------------------------------------------------------------------


def test_finds_a_tool_by_words_from_its_description(stocked: Registry) -> None:
    # tokens are joined with OR (the spec), so other tools mentioning "text" may follow; the right one must come first
    assert _names(stocked, "extract text from a pdf")[0] == "pdf_text"
    assert _names(stocked, "pdf") == ["pdf_text"]
    assert _names(stocked, "how many words are in this text")[0] == "word_count"


def test_finds_a_tool_by_words_from_its_examples(stocked: Registry) -> None:
    # "population" and "Dhaka" appear only in the example file and "rows" only in the example output
    assert _names(stocked, "population") == []  # file contents are not indexed
    assert _names(stocked, "rows") == ["csv_summary"]
    assert _names(stocked, "data.csv") == ["csv_summary"]  # the example file's name is


def test_finds_a_tool_by_words_from_when_to_use(stocked: Registry) -> None:
    assert _names(stocked, "tabular overview") == ["csv_summary"]


def test_a_tool_name_matches_as_separate_words(stocked: Registry) -> None:
    assert _names(stocked, "summary") == ["csv_summary"]
    assert _names(stocked, "csv") == ["csv_summary"]


def test_words_are_stemmed(stocked: Registry) -> None:
    assert _names(stocked, "counting")[0] == "word_count"
    assert _names(stocked, "summarising columns") == ["csv_summary"]


def test_case_and_punctuation_do_not_matter(stocked: Registry) -> None:
    assert _names(stocked, "PDF!!! (text?)")[0] == "pdf_text"


def test_a_name_match_outranks_a_passing_mention(
    registry: Registry, make_package: Make, activate: Activate
) -> None:
    activate(
        registry.add_version(
            make_package("pdf_text", "1.0.0", description="Extract the text of a PDF document."),
            created_by="a",
        )
    )
    activate(
        registry.add_version(
            make_package(
                "report_builder",
                "1.0.0",
                description="Build a report; it can embed text extracted from a pdf earlier.",
            ),
            created_by="a",
        )
    )
    hits = registry.search("pdf")
    assert [h.name for h in hits] == ["pdf_text", "report_builder"]
    assert hits[0].score > hits[1].score > 0


def test_a_hit_carries_what_a_caller_needs(stocked: Registry) -> None:
    (hit,) = stocked.search("pdf")
    assert (hit.name, hit.version, hit.status) == ("pdf_text", "1.0.0", "active")
    assert hit.description.startswith("Extract the plain text")
    assert hit.uses == 0 and hit.success_rate is None  # no calls yet: not measured, not zero
    version = stocked.active_version("pdf_text")
    assert version is not None
    for n in range(6):
        stocked.record_call(version.id, ok=n != 0, duration_ms=3)
    (hit,) = stocked.search("pdf")
    assert hit.uses == 6 and hit.success_rate == pytest.approx(5 / 6)


def test_limit_is_respected_and_clamped(stocked: Registry) -> None:
    assert len(stocked.search("text", limit=1)) == 1
    assert len(stocked.search("text pdf words column", limit=0)) == 1  # at least one
    assert len(stocked.search("text pdf words column", limit=10_000)) <= search.MAX_LIMIT
    assert len(stocked.search("text pdf words column")) <= search.DEFAULT_LIMIT


def test_nothing_matches_nothing(stocked: Registry) -> None:
    assert stocked.search("quantum chromodynamics") == []


# --- What is not found -----------------------------------------------------------------------------------------------


def test_only_active_versions_are_found(registry: Registry, make_package: Make, activate: Activate) -> None:
    submitted = registry.add_version(make_package("alpha_tool", "1.0.0"), created_by="a")
    assert registry.search("words") == []  # submitted
    registry.transition(submitted.id, "verifying", actor="pipeline")
    registry.transition(submitted.id, "verified", actor="pipeline")
    assert registry.search("words") == []  # verified but not active
    registry.transition(submitted.id, "active", actor="pipeline")
    assert _names(registry, "words") == ["alpha_tool"]
    registry.transition(submitted.id, "deprecated", actor="operator")
    assert registry.search("words") == []  # deprecated


def test_deprecated_versions_are_ignored_when_a_newer_one_is_active(
    registry: Registry, make_package: Make, activate: Activate
) -> None:
    activate(
        registry.add_version(
            make_package("word_count", "1.0.0", description="Old wording: tally the vocabulary of a text."),
            created_by="a",
        )
    )
    activate(
        registry.add_version(
            make_package("word_count", "2.0.0", description="New wording: count the words of a text."),
            created_by="a",
        )
    )
    assert registry.search("vocabulary") == []
    assert [(h.name, h.version) for h in registry.search("words")] == [("word_count", "2.0.0")]


def test_rejected_and_quarantined_tools_are_not_found(
    registry: Registry, make_package: Make, activate: Activate
) -> None:
    rejected = registry.add_version(make_package("bad_tool", "1.0.0"), created_by="a")
    registry.transition(rejected.id, "verifying", actor="pipeline")
    registry.transition(rejected.id, "rejected", actor="pipeline")
    quarantined = activate(registry.add_version(make_package("risky_tool", "1.0.0"), created_by="a"))
    registry.transition(quarantined.id, "quarantined", actor="pipeline", reason="violation")
    assert registry.search("words") == []


def test_a_stale_index_row_still_cannot_surface_an_inactive_version(
    registry: Registry, make_package: Make, activate: Activate
) -> None:
    """Defence in depth: even if the index drifted (a row left behind for a deprecated version), the query checks the status."""
    old = activate(registry.add_version(make_package("word_count", "1.0.0"), created_by="a"))
    registry.transition(old.id, "deprecated", actor="operator")
    with registry.db.transaction() as conn:  # plant the drift by hand
        conn.execute(
            "INSERT INTO tool_search (rowid, name, description) VALUES (?, 'word_count', 'count the words')",
            (old.id,),
        )
    assert registry.db.query_one("SELECT COUNT(*) FROM tool_search WHERE rowid = ?", (old.id,))[0] == 1
    assert registry.search("words") == []


# --- Hostile queries -------------------------------------------------------------------------------------------------

HOSTILE = [
    '" OR 1',
    "*",
    "NEAR(",
    "NEAR(a b, 2)",
    '"unterminated',
    "a AND NOT b",
    "name:word",
    "{name description}: words",
    "^words",
    "words -text",
    "word* OR text*",
    "'; DROP TABLE versions; --",
    "words)) OR ((",
    "\x00",
    "‮evil",
    "text" + " text" * 5000,
    "x" * 100_000,
    "",
    "   ",
    "!!!",
]


@pytest.mark.parametrize("query", HOSTILE)
def test_hostile_queries_do_not_raise(stocked: Registry, query: str) -> None:
    result = stocked.search(query)
    assert isinstance(result, list) and len(result) <= search.DEFAULT_LIMIT
    assert stocked.db.query_one("SELECT COUNT(*) FROM versions")[0] == 3  # nothing was dropped


def test_operators_are_words_not_syntax(stocked: Registry) -> None:
    assert _names(stocked, "pdf OR csv", limit=10) == ["csv_summary", "pdf_text"] or _names(
        stocked, "pdf OR csv", limit=10
    ) == [
        "pdf_text",
        "csv_summary",
    ]
    assert _names(stocked, "pdf NOT text") != []  # NOT is only a word here
    assert _names(stocked, "NEAR") == []


def test_a_query_is_reduced_to_at_most_twelve_plain_tokens() -> None:
    tokens = query_tokens(" ".join(f"w{n}" for n in range(40)))
    assert len(tokens) == MAX_QUERY_TOKENS == 12
    assert query_tokens('Hello, "World"! hello') == ["hello", "world"]
    assert query_tokens("word_count") == ["word", "count"]
    assert query_tokens("*** ---") == []
    assert query_tokens(None) == []  # type: ignore[arg-type]
    assert all(len(t) <= search.MAX_TOKEN_CHARS for t in query_tokens("y" * 500))


def test_every_token_is_quoted() -> None:
    assert fts_expression(["or", "near", "1"]) == '"or" OR "near" OR "1"'


# --- The fallback ----------------------------------------------------------------------------------------------------


@pytest.fixture
def no_fts(stocked: Registry, monkeypatch: pytest.MonkeyPatch) -> Registry:
    monkeypatch.setattr(search, "_warned_no_fts", False)
    stocked.db.fts_available = False
    return stocked


def test_fallback_finds_by_description_name_and_examples(no_fts: Registry) -> None:
    assert _names(no_fts, "extract text from a pdf")[0] == "pdf_text"
    assert _names(no_fts, "csv") == ["csv_summary"]
    assert _names(no_fts, "rows") == ["csv_summary"]
    assert _names(no_fts, "tabular overview") == ["csv_summary"]
    assert no_fts.search("quantum chromodynamics") == []


def test_fallback_ranks_by_overlap_and_orders_by_score(no_fts: Registry) -> None:
    hits = no_fts.search("pdf text pages", limit=10)
    assert hits[0].name == "pdf_text"
    assert all(h.score > 0 for h in hits)
    assert [h.score for h in hits] == sorted((h.score for h in hits), reverse=True)


def test_fallback_ignores_inactive_versions(no_fts: Registry) -> None:
    version = no_fts.active_version("pdf_text")
    assert version is not None
    no_fts.transition(version.id, "deprecated", actor="operator")
    assert no_fts.search("pdf") == []


@pytest.mark.parametrize("query", HOSTILE)
def test_fallback_survives_hostile_queries(no_fts: Registry, query: str) -> None:
    assert isinstance(no_fts.search(query), list)


def test_the_fallback_warns_once(no_fts: Registry, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="toolforge.registry.search"):
        no_fts.search("pdf")
        no_fts.search("words")
        no_fts.search("csv")
    warnings = [r for r in caplog.records if "token-overlap fallback" in r.getMessage()]
    assert len(warnings) == 1


def test_a_sqlite_without_fts5_is_detected_and_search_still_works(
    tmp_path, make_package: Make, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Simulate a SQLite build with no FTS5 by asking for a tokenizer module that does not exist."""
    from toolforge.registry import db as db_module

    monkeypatch.setattr(db_module, "_FTS_DDL", "CREATE VIRTUAL TABLE tool_search USING fts9(name)")
    monkeypatch.setattr(search, "_warned_no_fts", False)
    with caplog.at_level(logging.WARNING), Registry(tmp_path / "home") as registry:
        assert registry.db.fts_available is False
        record = registry.add_version(make_package(), created_by="agent")
        for status in ("verifying", "verified", "active"):
            registry.transition(record.id, status, actor="pipeline")  # indexing is skipped, not an error
        assert _names(registry, "count the words") == ["word_count"]
    assert any("no FTS5" in r.getMessage() for r in caplog.records)
