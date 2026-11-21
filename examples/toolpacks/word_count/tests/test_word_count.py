from tool import run


def test_counts_words_characters_and_lines():
    result = run({"text": "the cat and the hat"})
    assert result["words"] == 5
    assert result["characters"] == 19
    assert result["lines"] == 1


def test_empty_text_is_all_zero():
    assert run({"text": ""}) == {"words": 0, "characters": 0, "lines": 0, "top_words": []}


def test_top_words_rank_by_count_then_alphabetically():
    result = run({"text": "b a b c a b", "top": 2})
    assert result["top_words"] == [{"word": "b", "count": 3}, {"word": "a", "count": 2}]


def test_case_and_punctuation_do_not_split_words():
    result = run({"text": "Don't stop. DON'T stop!"})
    assert result["words"] == 4
    assert result["top_words"][0] == {"word": "don't", "count": 2}


def test_lines_counts_each_line_once():
    assert run({"text": "one\ntwo\nthree\n"})["lines"] == 3


def test_a_very_long_word_is_shown_shortened_but_still_counted_in_full():
    result = run({"text": "y" * 5000})
    assert result["top_words"] == [{"word": "y" * 64, "count": 1}]
    assert result["characters"] == 5000
