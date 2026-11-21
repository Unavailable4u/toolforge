"""Count words, characters and lines in text, and list the most frequent words."""

from __future__ import annotations

import re
from collections import Counter

_WORD = re.compile(r"[^\W_]+(?:'[^\W_]+)*", re.UNICODE)
# A word is shown at most this long. Without the cap, one 50 000-character word (the schema allows it) would be echoed back and the
# result would be larger than limits.output_kb; the schema fuzz gate (G4) found exactly that.
MAX_SHOWN_CHARS = 64


def run(args: dict) -> dict:
    text = args["text"]
    top = args.get("top", 3)
    words = [match.group(0).lower() for match in _WORD.finditer(text)]
    # Most frequent first; ties are broken alphabetically so the answer is deterministic.
    ranked = sorted(Counter(words).items(), key=lambda item: (-item[1], item[0]))
    return {
        "words": len(words),
        "characters": len(text),
        "lines": len(text.splitlines()),
        "top_words": [{"word": word[:MAX_SHOWN_CHARS], "count": count} for word, count in ranked[:top]],
    }
