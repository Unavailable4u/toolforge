"""Find secret-shaped strings in text, and say where, never what.

One constant list of named patterns (`PATTERNS`) and one function (`scan`). Gate G1 runs it over every file of a package; the LLM
client (P10) runs it over a whole prompt as a last check before anything is sent to a provider. A hit reports the pattern's name,
the file and the line, and nothing else: the matched text is never in a `Finding`, so it cannot reach a report, a hint, a ledger
event or a log line.

Every pattern is linear in the size of the text (no nested repetition, bounded gaps), because the text is hostile input: a file made
to stall a scanner must not stall the pipeline. This is an early warning, not a guarantee: a secret that is split, encoded or
named in an unusual way is not found, and an ordinary value that looks like a secret may be (the hint says how to avoid that).

Proven by `tests/test_secretscan.py`: each pattern is detected, near misses and ordinary hashes, UUIDs and base64 data are not, the
matched text never appears in a finding, and a hostile text is scanned in bounded time.
"""

from __future__ import annotations

import bisect
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, NamedTuple

# One scan reports at most this many findings, so a file full of tokens cannot grow a report without bound.
MAX_FINDINGS = 200
# Text beyond this is not scanned (a package is far smaller; a prompt is smaller still).
MAX_SCAN_CHARS = 4_000_000
# Bits per character; random base62 is about 4.1, words and paths are lower.
_MIN_ENTROPY = 3.5
# MD5, SHA-1, SHA-224, SHA-256, SHA-384 and SHA-512 written in hex.
_HASH_LENGTHS = frozenset({32, 40, 56, 64, 96, 128})
_PLACEHOLDER_WORDS = ("example", "placeholder", "changeme", "dummy", "sample", "your")
_HEX = re.compile(r"[0-9a-fA-F]+")
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


@dataclass(frozen=True)
class Finding:
    """Where a secret-shaped string is. Deliberately has no field for the text itself."""

    pattern: str
    file: str
    line: int

    def as_dict(self) -> dict[str, Any]:
        return {"pattern": self.pattern, "file": self.file, "line": self.line}


class SecretPattern(NamedTuple):
    name: str  # stable; appears in reports, hints and the docs
    description: str  # one line, as shown in the docs
    regex: re.Pattern[str]
    # A second look at a match; None accepts every match.
    accepts: Callable[[re.Match[str]], bool] | None = None


def _entropy(value: str) -> float:
    counts: dict[str, int] = {}
    for char in value:
        counts[char] = counts.get(char, 0) + 1
    size = len(value)
    return -sum(n / size * math.log2(n / size) for n in counts.values())


def _classes(value: str) -> int:
    return sum(
        (
            any(c.islower() for c in value),
            any(c.isupper() for c in value),
            any(c.isdigit() for c in value),
        )
    )


def _looks_like_a_secret_value(match: re.Match[str]) -> bool:
    """A quoted value assigned to a name such as `api_key`: long, mixed and random, not a hash, a UUID or a placeholder."""
    value = match.group("value")
    if _UUID.fullmatch(value) or (_HEX.fullmatch(value) and len(value) in _HASH_LENGTHS):
        return False  # a UUID or a digest: common in test data and not a credential by itself
    lowered = value.lower()
    if any(word in lowered for word in _PLACEHOLDER_WORDS):
        return False
    return _classes(value) >= 2 and _entropy(value) >= _MIN_ENTROPY


# `\w` also matches digits and `_`, so `(?<![A-Za-z0-9])` is used where a token may follow an underscore or sit inside a name.
PATTERNS: tuple[SecretPattern, ...] = (
    SecretPattern(
        "aws_access_key_id",
        "An AWS access key id: `AKIA` followed by 16 upper-case letters or digits.",
        re.compile(r"(?<![A-Za-z0-9])AKIA[0-9A-Z]{16}(?![A-Za-z0-9])"),
    ),
    SecretPattern(
        "github_token",
        "A GitHub token with a `ghp_`, `gho_`, `ghu_`, `ghs_` or `ghr_` prefix.",
        re.compile(r"(?<![A-Za-z0-9_])gh[pousr]_[A-Za-z0-9]{36,255}(?![A-Za-z0-9])"),
    ),
    SecretPattern(
        "github_fine_grained_token",
        "A GitHub fine-grained token: `github_pat_` followed by at least 22 letters, digits or underscores.",
        re.compile(r"(?<![A-Za-z0-9_])github_pat_[A-Za-z0-9_]{22,255}(?![A-Za-z0-9_])"),
    ),
    SecretPattern(
        "slack_token",
        "A Slack token: `xoxb-`, `xoxa-`, `xoxp-`, `xoxr-` or `xoxs-` followed by at least 10 characters.",
        re.compile(r"(?<![A-Za-z0-9])xox[baprs]-[0-9A-Za-z-]{10,255}"),
    ),
    SecretPattern(
        "google_api_key",
        "A Google API key: `AIza` followed by 35 letters, digits, `_` or `-`.",
        re.compile(r"(?<![A-Za-z0-9_-])AIza[0-9A-Za-z_-]{35}(?![A-Za-z0-9_-])"),
    ),
    SecretPattern(
        "private_key_header",
        "The first line of a PEM private key (`-----BEGIN ... PRIVATE KEY-----`).",
        re.compile(r"-----BEGIN (?:[A-Z0-9]{1,16} ){0,3}PRIVATE KEY(?: BLOCK)?-----"),
    ),
    SecretPattern(
        "secret_assignment",
        "A name containing `key`, `token`, `secret` or `password` assigned a quoted value of 20 or more mixed, "
        "random-looking characters. Hashes, UUIDs and placeholders are not flagged.",
        re.compile(
            r"(?:key|token|secret|passw(?:or)?d)[\w.\-]{0,40}[\"']?[ \t]*[:=][ \t]*[\"'](?P<value>[A-Za-z0-9+/=_\-.~]{20,512})[\"']",
            re.IGNORECASE,
        ),
        _looks_like_a_secret_value,
    ),
)
PATTERN_NAMES: tuple[str, ...] = tuple(pattern.name for pattern in PATTERNS)


def patterns_markdown() -> str:
    """The pattern list as a Markdown table. `docs/verification.md` embeds exactly this text (drift-tested)."""
    rows = ["| Pattern | What it looks for |", "| --- | --- |"]
    rows += [f"| `{pattern.name}` | {pattern.description} |" for pattern in PATTERNS]
    return "\n".join(rows)


def _as_text(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="replace")
    return value if isinstance(value, str) else ("" if value is None else str(value))


def scan(text: Any, file: str = "") -> list[Finding]:
    """Every secret-shaped string in `text`, as `(pattern, file, line)`, in line order. Never returns the matched text.

    `file` is only a label copied into each finding. At most `MAX_FINDINGS` are returned and at most `MAX_SCAN_CHARS` characters are
    read; a finding is reported once per pattern and line.
    """
    body = _as_text(text)[:MAX_SCAN_CHARS]
    newlines: list[int] | None = None
    found: dict[tuple[str, int], Finding] = {}
    for pattern in PATTERNS:
        for match in pattern.regex.finditer(body):
            if pattern.accepts is not None and not pattern.accepts(match):
                continue
            if newlines is None:
                newlines = [at for at, char in enumerate(body) if char == "\n"]
            line = bisect.bisect_left(newlines, match.start()) + 1
            found.setdefault((pattern.name, line), Finding(pattern.name, file, line))
            if len(found) >= MAX_FINDINGS:
                return sorted(found.values(), key=lambda f: (f.line, f.pattern))
    return sorted(found.values(), key=lambda f: (f.line, f.pattern))


__all__ = [
    "MAX_FINDINGS",
    "PATTERNS",
    "PATTERN_NAMES",
    "Finding",
    "SecretPattern",
    "patterns_markdown",
    "scan",
]
