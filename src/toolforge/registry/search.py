"""Search over active tools: an FTS5 index with bm25 ranking, and a plain-Python fallback.

The query is hostile input (it comes from an agent). `query_tokens` reduces it to at most `MAX_QUERY_TOKENS` alphanumeric
words and every word is quoted before it reaches SQLite, so no FTS operator (`OR`, `NEAR(`, `*`, `-`, `:`, a quote) can
change what the query means. Proved by `tests/test_registry_search.py::test_hostile_queries_do_not_raise`.

Only `active` versions are indexed. The index is kept in step by `lifecycle.transition`, inside the same transaction as the
status change, so search can never list a version that is not active (and the query joins `versions` and checks again).

Ranking here is plain bm25 with fixed column weights. Reuse ranking (success rate, recency, permissions) is patch P12.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from toolforge.registry.db import Database, VersionRecord, call_stats, row_to_version

log = logging.getLogger(__name__)

DEFAULT_LIMIT = 5
MAX_LIMIT = 50
MAX_QUERY_TOKENS = 12
MAX_TOKEN_CHARS = 64
MAX_EXAMPLES_CHARS = 4000  # how much example text is indexed per version
BM25_WEIGHTS = (10.0, 4.0, 2.0, 1.0)  # name, description, when_to_use, examples_text

# `_` is not part of a word: the FTS tokenizer splits on it, so the query must too (`word_count` is two words).
_WORD = re.compile(r"[^\W_]+")


def query_tokens(query: str) -> list[str]:
    """Plain lower-case words from `query`: letters and digits only, no repeats, at most MAX_QUERY_TOKENS of them."""
    seen: dict[str, None] = {}
    if isinstance(query, str):
        for word in _WORD.findall(query.lower()):
            seen.setdefault(word[:MAX_TOKEN_CHARS], None)
            if len(seen) >= MAX_QUERY_TOKENS:
                break
    return list(seen)


def fts_expression(tokens: Iterable[str]) -> str:
    """A MATCH expression: each token quoted, joined by OR. Tokens hold no quote character, so quoting is airtight."""
    return " OR ".join(f'"{token}"' for token in tokens)


# --- What is indexed -----------------------------------------------------------------------------------------------------


def _example_text(manifest: dict[str, Any]) -> str:
    """Words from the examples: object keys and text values of each input and output, and the names of example files."""
    examples = manifest.get("examples")
    pieces: list[str] = []
    stack: list[Any] = []
    if isinstance(examples, list):
        for example in examples:
            if isinstance(example, dict):
                stack.extend([example.get("output"), example.get("input")])
                files = example.get("files")
                if isinstance(files, dict):
                    pieces.extend(str(path) for path in files)
    size = sum(len(piece) for piece in pieces)
    while stack and size < MAX_EXAMPLES_CHARS:
        node = stack.pop()
        if isinstance(node, str):
            pieces.append(node)
            size += len(node)
        elif isinstance(node, dict):
            for key, child in node.items():
                pieces.append(str(key))
                size += len(str(key))
                stack.append(child)
        elif isinstance(node, list):
            stack.extend(node)
    return " ".join(pieces)[:MAX_EXAMPLES_CHARS]


def _fields(record: VersionRecord) -> tuple[str, str, str, str]:
    manifest = record.manifest
    return (
        record.name,
        str(manifest.get("description", "")),
        str(manifest.get("when_to_use", "")),
        _example_text(manifest),
    )


def index_version(db: Database, record: VersionRecord) -> None:
    """Add or refresh the search row of a version. Call inside a transaction; a no-op without FTS5."""
    if not db.fts_available:
        return
    with db.transaction() as conn:
        conn.execute("DELETE FROM tool_search WHERE rowid = ?", (record.id,))
        conn.execute(
            "INSERT INTO tool_search (rowid, name, description, when_to_use, examples_text) VALUES (?, ?, ?, ?, ?)",
            (record.id, *_fields(record)),
        )


def remove_version(db: Database, version_id: int) -> None:
    """Drop the search row of a version that is no longer active. Call inside a transaction; a no-op without FTS5."""
    if not db.fts_available:
        return
    with db.transaction() as conn:
        conn.execute("DELETE FROM tool_search WHERE rowid = ?", (version_id,))


# --- Searching -----------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Hit:
    """One search result. `score` is higher for a better match; scores of the two back ends are not comparable."""

    name: str
    version: str
    description: str
    score: float
    status: str
    uses: int
    success_rate: float | None  # None means "not measured" (too few calls)


_warned_no_fts = False


def _warn_no_fts() -> None:
    global _warned_no_fts
    if not _warned_no_fts:
        _warned_no_fts = True
        log.warning("registry search is using the token-overlap fallback because FTS5 is not available")


def _hit(db: Database, record: VersionRecord, score: float) -> Hit:
    stats = call_stats(db, record.id)
    return Hit(
        name=record.name,
        version=record.version,
        description=str(record.manifest.get("description", "")),
        score=score,
        status=record.status,
        uses=stats.uses,
        success_rate=stats.success_rate,
    )


def _search_fts(db: Database, tokens: list[str], limit: int) -> list[Hit]:
    weights = ", ".join(str(w) for w in BM25_WEIGHTS)
    rows = db.query(
        f"SELECT v.*, bm25(tool_search, {weights}) AS rank FROM tool_search "
        "JOIN versions v ON v.id = tool_search.rowid "
        "WHERE tool_search MATCH ? AND v.status = 'active' ORDER BY rank, v.name LIMIT ?",
        (fts_expression(tokens), limit),
    )
    return [_hit(db, row_to_version(row), -float(row["rank"])) for row in rows]  # bm25 is lower-is-better


def _search_overlap(db: Database, tokens: list[str], limit: int) -> list[Hit]:
    """Without FTS5: score each active tool by how many query words it contains, weighting name over description over examples."""
    scored: list[tuple[float, VersionRecord]] = []
    field_weights = (4.0, 2.0, 2.0, 1.0)
    for row in db.query("SELECT * FROM versions WHERE status = 'active'"):
        record = row_to_version(row)
        words = [set(_WORD.findall(text.lower())) for text in _fields(record)]
        total = 0.0
        for token in tokens:
            total += max(
                (w for w, field in zip(field_weights, words, strict=True) if token in field), default=0.0
            )
        if total > 0:
            scored.append((total / (max(field_weights) * len(tokens)), record))
    scored.sort(key=lambda item: (-item[0], item[1].name))
    return [_hit(db, record, score) for score, record in scored[:limit]]


def search(db: Database, query: str, limit: int = DEFAULT_LIMIT) -> list[Hit]:
    """The best active tools for `query`, best first. An empty or all-punctuation query finds nothing and does not raise."""
    tokens = query_tokens(query)
    if not tokens:
        return []
    limit = max(1, min(int(limit), MAX_LIMIT))
    if db.fts_available:
        try:
            return _search_fts(db, tokens, limit)
        except (
            sqlite3.OperationalError
        ) as exc:  # should be impossible with quoted tokens; never let it reach a caller
            log.warning("FTS5 search failed (%s); using the token-overlap fallback", exc)
    _warn_no_fts()
    return _search_overlap(db, tokens, limit)
