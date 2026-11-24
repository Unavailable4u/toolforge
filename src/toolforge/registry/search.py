"""Search over active tools: an FTS5 index with bm25 ranking, and a plain-Python fallback.

The query is hostile input (it comes from an agent). `query_tokens` reduces it to at most `MAX_QUERY_TOKENS` alphanumeric
words and every word is quoted before it reaches SQLite, so no FTS operator (`OR`, `NEAR(`, `*`, `-`, `:`, a quote) can
change what the query means. Proved by `tests/test_registry_search.py::test_hostile_queries_do_not_raise`.

Only `active` versions are indexed. The index is kept in step by `lifecycle.transition`, inside the same transaction as the
status change, so search can never list a version that is not active (and the query joins `versions` and checks again).

Ranking (P12). SQLite's bm25 (fixed column weights) finds the candidates; `rank` then orders them with the text match, the overlap between
the query and the example inputs, a smoothed success rate, a mild recency term and a tie-break for narrower permissions. Every number
in that formula, and every threshold behind `confidence`, is in the one constant block `RANKING`, and `tests/test_search_ranking.py`
pins what each one does. The track record can only move a tool among tools that already match: it is a small share of the score, and a
tool that matches none of the query's words is not a candidate at all. `confidence` is not a quality score. It says how much of
what was asked the tool's own description covers, and demotes a tool whose recent calls mostly fail.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from toolforge.registry.db import Database, VersionRecord, call_stats, row_to_version

log = logging.getLogger(__name__)

DEFAULT_LIMIT = 5
MAX_LIMIT = 50
MAX_QUERY_TOKENS = 12
MAX_TOKEN_CHARS = 64
MAX_EXAMPLES_CHARS = 4000  # how much example text is indexed per version
BM25_WEIGHTS = (10.0, 4.0, 2.0, 1.0)  # name, description, when_to_use, examples_text
MAX_EXAMPLE_INPUT_CHARS = 2000  # how much of the example inputs is read for the overlap term


@dataclass(frozen=True)
class Ranking:
    """The constants of the ranking and of `confidence`, in one place. Changing one is a decision: the tests name what it does."""

    candidates: int = 50  # how many bm25 matches are re-ranked
    text_bm25_share: float = (
        0.6  # of the text term: the rest is the share of the query's words the description covers
    )
    w_text: float = 1.0
    w_examples: float = 0.20  # query words that also occur in the example inputs
    w_reliability: float = 0.30  # times (smoothed success rate - prior mean); negative for a tool that fails
    w_recency: float = 0.05
    w_narrow: float = 0.03  # a tie-break: fewer hosts and secrets beat more
    prior_ok: float = (
        3.0  # Beta(3, 1) prior on the success rate: a tool with no calls is 0.75, one lucky call only 0.8
    )
    prior_bad: float = 1.0
    recency_half_life_days: float = 60.0
    high_coverage: float = 0.75  # `high`: at least this share of the query's words is in the tool's name, description or when_to_use
    medium_coverage: float = 0.40
    min_reliability_for_high: float = (
        0.60  # a smoothed success rate below this keeps a good match at `medium`
    )

    @property
    def prior_mean(self) -> float:
        return self.prior_ok / (self.prior_ok + self.prior_bad)


RANKING = Ranking()
CONFIDENCES = ("high", "medium", "low")

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
    confidence: str = "low"  # high, medium or low: see `confidence_of`
    why: str = ""  # one line of numbers: words covered, recent calls, permissions. No author text.
    coverage: float = 0.0  # the share of the query's words the description covers


_warned_no_fts = False


def _warn_no_fts() -> None:
    global _warned_no_fts
    if not _warned_no_fts:
        _warned_no_fts = True
        log.warning("registry search is using the token-overlap fallback because FTS5 is not available")


# --- The ingredients of the ranking -----------------------------------------------------------------------------------------


def stem(word: str) -> str:
    """A very small, deterministic stemmer, so that `counting` and `counts` meet `count`. Used on both sides of every comparison."""
    for suffix, keep in (("ies", "y"), ("ing", ""), ("ed", ""), ("es", ""), ("s", "")):
        if (
            word.endswith(suffix)
            and not (suffix == "s" and word.endswith("ss"))
            and len(word) - len(suffix) >= 3
        ):
            return word[: -len(suffix)] + keep
    return word


def _stems(text: str) -> set[str]:
    return {stem(word) for word in _WORD.findall(text.lower())}


def _purpose_words(record: VersionRecord) -> set[str]:
    """Stems of the name, description and when_to_use: what the author says the tool is for."""
    manifest = record.manifest
    return _stems(f"{record.name} {manifest.get('description', '')} {manifest.get('when_to_use', '')}")


def coverage_of(tokens: Sequence[str], record: VersionRecord) -> float:
    """The share of the query's words (0 to 1) that appear in the tool's name, description or when_to_use."""
    if not tokens:
        return 0.0
    have = _purpose_words(record)
    return sum(1 for token in tokens if stem(token) in have) / len(tokens)


def example_input_words(manifest: Mapping[str, Any]) -> set[str]:
    """Stems of the keys and text values of the manifest's example *inputs* (not their outputs), at most 2000 characters of them."""
    pieces: list[str] = []
    stack: list[Any] = []
    examples = manifest.get("examples")
    if isinstance(examples, list):
        stack.extend(e.get("input") for e in examples if isinstance(e, dict))
    size = 0
    while stack and size < MAX_EXAMPLE_INPUT_CHARS:
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
    return _stems(" ".join(pieces)[:MAX_EXAMPLE_INPUT_CHARS])


def smoothed_success(recent_ok: int, recent_calls: int, ranking: Ranking = RANKING) -> float:
    """The success rate with a Beta prior: `(ok + a) / (calls + a + b)`. With no calls it is the prior mean, so one lucky call barely moves it."""
    return (recent_ok + ranking.prior_ok) / (recent_calls + ranking.prior_ok + ranking.prior_bad)


def _recency(record: VersionRecord, now: str, ranking: Ranking = RANKING) -> float:
    """1.0 for a version that became active just now, 0.5 after the half-life, and so on. A timestamp that cannot be read counts as 0."""
    try:
        then = datetime.strptime(record.updated_at, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
        moment = datetime.strptime(now, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except ValueError:
        return 0.0
    days = max(0.0, (moment - then).total_seconds() / 86400)
    return 0.5 ** (days / ranking.recency_half_life_days)


def _narrowness(record: VersionRecord) -> float:
    """1.0 for a tool that asks for no network and no secrets; less for each host, and twice as much for each secret."""
    perms = record.permissions
    cost = len(perms.get("network") or []) + 2 * len(perms.get("secrets") or [])
    return 1.0 / (1.0 + cost)


def confidence_of(coverage: float, reliability: float, ranking: Ranking = RANKING) -> str:
    """`high`, `medium` or `low`. It is how much of the request the description covers, not how good the tool is. A tool that covers
    the request but whose recent calls mostly fail (a smoothed success rate under `min_reliability_for_high`) is `medium` at best."""
    if coverage >= ranking.high_coverage and reliability >= ranking.min_reliability_for_high:
        return "high"
    if coverage >= ranking.medium_coverage:
        return "medium"
    return "low"


def text_confidence(query: str, record: VersionRecord) -> str:
    """`confidence_of` from the text alone, for callers that have no call statistics at hand (a track record is then neutral)."""
    return confidence_of(coverage_of(query_tokens(query), record), RANKING.prior_mean)


def _why(coverage: float, token_count: int, stats: Any, record: VersionRecord) -> str:
    """One line made only of numbers and fixed words: no text an author wrote can get into it."""
    covered = round(coverage * token_count)
    if stats.success_rate is None:
        track = (
            "no calls yet"
            if stats.recent_calls == 0
            else f"{stats.recent_calls} recent calls, too few to rate"
        )
    else:
        track = f"{stats.recent_ok} of {stats.recent_calls} recent calls ok"
    perms = record.permissions
    hosts, secrets = len(perms.get("network") or []), len(perms.get("secrets") or [])
    asks = (
        "no network or secrets" if not hosts and not secrets else f"asks for {hosts} hosts, {secrets} secrets"
    )
    return f"covers {covered} of {token_count} of your words; {track}; {asks}"


# --- Searching -----------------------------------------------------------------------------------------------------------


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


def _candidates_fts(db: Database, tokens: list[str], pool: int) -> list[tuple[VersionRecord, float]]:
    """The best `pool` active matches by bm25, each with a relevance (higher is better)."""
    weights = ", ".join(str(w) for w in BM25_WEIGHTS)
    rows = db.query(
        f"SELECT v.*, bm25(tool_search, {weights}) AS rank FROM tool_search "
        "JOIN versions v ON v.id = tool_search.rowid "
        "WHERE tool_search MATCH ? AND v.status = 'active' ORDER BY rank, v.name LIMIT ?",
        (fts_expression(tokens), pool),
    )
    return [(row_to_version(row), -float(row["rank"])) for row in rows]  # bm25 is lower-is-better


def _candidates_overlap(db: Database, tokens: list[str], pool: int) -> list[tuple[VersionRecord, float]]:
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
    return [(record, score) for score, record in scored[:pool]]


def rank(
    db: Database,
    tokens: list[str],
    candidates: list[tuple[VersionRecord, float]],
    ranking: Ranking = RANKING,
) -> list[Hit]:
    """Order `candidates` (record, relevance) best first and give each a confidence and a one-line reason.

    score = w_text * (share * bm25 / best bm25 + (1 - share) * coverage) + w_examples * example overlap
            + w_reliability * (smoothed success - prior mean) + w_recency * recency + w_narrow * narrowness

    Ties are broken by name, so the order never depends on the database's row order.
    """
    best = max((relevance for _, relevance in candidates), default=0.0)
    now = db.now()
    wanted = {stem(token) for token in tokens}
    hits: list[Hit] = []
    for record, relevance in candidates:
        stats = call_stats(db, record.id)
        coverage = coverage_of(tokens, record)
        bm25 = relevance / best if best > 0 else 0.0
        text = ranking.text_bm25_share * bm25 + (1.0 - ranking.text_bm25_share) * coverage
        overlap = len(wanted & example_input_words(record.manifest)) / len(wanted) if wanted else 0.0
        reliability = smoothed_success(stats.recent_ok, stats.recent_calls, ranking)
        score = (
            ranking.w_text * text
            + ranking.w_examples * overlap
            + ranking.w_reliability * (reliability - ranking.prior_mean)
            + ranking.w_recency * _recency(record, now, ranking)
            + ranking.w_narrow * _narrowness(record)
        )
        hits.append(
            Hit(
                name=record.name,
                version=record.version,
                description=str(record.manifest.get("description", "")),
                score=round(score, 9),
                status=record.status,
                uses=stats.uses,
                success_rate=stats.success_rate,
                confidence=confidence_of(coverage, reliability, ranking),
                why=_why(coverage, len(tokens), stats, record),
                coverage=round(coverage, 6),
            )
        )
    hits.sort(key=lambda hit: (-hit.score, hit.name))
    return hits


def search(db: Database, query: str, limit: int = DEFAULT_LIMIT) -> list[Hit]:
    """The best active tools for `query`, best first. An empty or all-punctuation query finds nothing and does not raise."""
    tokens = query_tokens(query)
    if not tokens:
        return []
    limit = max(1, min(int(limit), MAX_LIMIT))
    pool = max(limit, RANKING.candidates)
    candidates: list[tuple[VersionRecord, float]] | None = None
    if db.fts_available:
        try:
            candidates = _candidates_fts(db, tokens, pool)
        except (
            sqlite3.OperationalError
        ) as exc:  # should be impossible with quoted tokens; never let it reach a caller
            log.warning("FTS5 search failed (%s); using the token-overlap fallback", exc)
    if candidates is None:
        _warn_no_fts()
        candidates = _candidates_overlap(db, tokens, pool)
    return rank(db, tokens, candidates)[:limit]
