"""P20: the authored words of the tour. Both variants exist for every step, the simple ones stay plain, and every reference resolves."""

from __future__ import annotations

import json
import re

import pytest
from site_helpers import ROOT, SITE, script

from toolforge.server.tools import META_TOOLS

tour = script("build_tour_data")
build_site = script("build_site")
CAPTIONS = json.loads((SITE / "src" / "tour_captions.json").read_text(encoding="utf-8"))
GLOSSARY = json.loads((SITE / "src" / "glossary.json").read_text(encoding="utf-8"))
#: The terms the P20 patch file lists.
SPEC_TERMS = {
    "mcp",
    "sandbox",
    "manifest",
    "schema_fuzzing",
    "egress",
    "decoy_credentials",
    "ledger",
    "hash_chain",
    "quarantine",
    "operator",
    "blast_radius",
}
#: Words a simple caption may use only as a marked glossary term, where the visitor can open an explanation.
JARGON = (
    r"mcp",
    r"sandbox\w*",
    r"manifest\w*",
    r"schema\w*",
    r"fuzz\w*",
    r"egress",
    r"decoy\w*",
    r"credential\w*",
    r"ledger\w*",
    r"hash\w*",
    r"quarantin\w*",
    r"operator\w*",
    r"blast radius",
    r"registry",
    r"container\w*",
    r"gates?",
    r"stubs?",
    r"api",
    r"repository",
    r"payload\w*",
    r"runtime",
    r"blob\w*",
    r"verif\w*",
)
#: About ten seconds of reading.
MAX_WORDS = 45
MARKUP = re.compile(r"\[\[([a-z_]+)(?:\|([^\]]+))?\]\]")
STEPS = list(tour.STEP_IDS)


def _plain(text: str) -> str:
    """The caption as it reads, terms replaced by the words shown."""
    return MARKUP.sub(lambda m: m[2] or GLOSSARY[m[1]]["term"], text)


def test_there_is_one_entry_for_every_step_in_order() -> None:
    assert list(CAPTIONS) == STEPS


@pytest.mark.parametrize("step", STEPS)
def test_every_step_has_both_variants_a_title_and_a_document(step: str) -> None:
    entry = CAPTIONS[step]
    assert set(entry) == {"title", "simple", "technical", "terms", "doc"}
    for key in ("title", "simple", "technical", "doc"):
        assert entry[key].strip(), key
    assert (ROOT / entry["doc"]).is_file(), entry["doc"]


@pytest.mark.parametrize("step", STEPS)
def test_the_simple_caption_uses_short_sentences(step: str) -> None:
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", _plain(CAPTIONS[step]["simple"])) if s]
    average = sum(len(s.split()) for s in sentences) / len(sentences)
    assert average <= 20, f"{average:.1f} words a sentence"


@pytest.mark.parametrize("step", STEPS)
def test_each_caption_can_be_read_in_about_ten_seconds(step: str) -> None:
    for variant in ("simple", "technical"):
        words = len(_plain(CAPTIONS[step][variant]).split())
        assert words <= MAX_WORDS, f"{variant}: {words} words"


@pytest.mark.parametrize("step", STEPS)
def test_the_simple_caption_has_no_unexplained_jargon(step: str) -> None:
    unmarked = MARKUP.sub(" ", CAPTIONS[step]["simple"]).lower()
    assert "`" not in unmarked, "no code in the simple words"
    found = [word for word in JARGON if re.search(rf"\b(?:{word})\b", unmarked)]
    assert not found, f"mark these as glossary terms or say them plainly: {found}"


def test_the_jargon_check_catches_an_unmarked_word() -> None:
    assert any(re.search(rf"\b(?:{w})\b", "it runs in a sandbox") for w in JARGON)


@pytest.mark.parametrize("step", STEPS)
def test_the_listed_terms_are_the_ones_the_captions_use_and_all_resolve(step: str) -> None:
    entry = CAPTIONS[step]
    used = {m[1] for variant in ("simple", "technical") for m in MARKUP.finditer(entry[variant])}
    assert used == set(entry["terms"])
    assert used <= set(GLOSSARY)
    for variant in ("simple", "technical"):
        left = MARKUP.sub("", entry[variant])
        assert "[[" not in left and "]]" not in left, "a broken glossary mark"
        assert left.count("`") % 2 == 0, "an unclosed code span"


def test_the_glossary_holds_the_listed_terms_and_each_is_reachable() -> None:
    assert set(GLOSSARY) == SPEC_TERMS
    for ident, entry in GLOSSARY.items():
        assert set(entry) == {"term", "plain", "analogy"}, ident
        assert all(value.strip() for value in entry.values()), ident
        assert any(ident in step["terms"] for step in CAPTIONS.values()), f"no caption opens {ident}"


@pytest.mark.parametrize("step", STEPS)
def test_the_technical_caption_names_real_things(step: str) -> None:
    text = CAPTIONS[step]["technical"]
    spans = re.findall(r"`([^`]+)`", text)
    assert spans, "name the file, the tool or the check"
    for span in spans:
        if span.endswith(".py"):
            assert (ROOT / "src" / "toolforge" / span).is_file() or (ROOT / span).is_file(), span
        elif re.fullmatch(r"forge_[a-z_]+", span):
            assert span in {tool.name for tool in META_TOOLS}, span
    for gate in tour.STEP_GATES.get(step, ()):
        if step != "caught":
            assert gate in text, f"{step} does not name {gate}"


def test_no_hype_words() -> None:
    text = json.dumps([CAPTIONS, GLOSSARY]).lower()
    for banned in ("revolutionary", "enterprise-grade", "military-grade", "seamless", "cutting-edge"):
        assert banned not in text


def test_the_written_list_escapes_what_it_is_given() -> None:
    shown = build_site.caption_html("a <b> `x<y` [[sandbox]] [[ledger|<i>book</i>]]", GLOSSARY)
    assert shown == "a &lt;b&gt; <code>x&lt;y</code> sandbox &lt;i&gt;book&lt;/i&gt;"
