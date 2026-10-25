"""Pattern builder: pure. Semantics are cross-checked with Python's re by mapping the POSIX
classes Oracle uses onto \\w / \\W (equivalent for ASCII); real Oracle behaviour is covered
by the integration tests."""
import re

import pytest

from scout.pattern import build_regex
from scout.search import SearchOptions


def matches(text, *terms, **kw):
    pattern, flags = build_regex(SearchOptions(terms=terms, **kw))
    py = pattern.replace("[[:alnum:]_]", r"\w").replace("[^[:alnum:]_]", r"\W")
    return re.search(py, text, re.I if flags == "i" else 0) is not None


def test_default_matches_inside_longer_words_without_wildcards():
    assert matches("The REPORT is due", "report")
    assert matches("Reportedly late", "report")  # prefix
    assert matches("a subreport", "report")  # suffix
    assert matches("Gilbert and Talbert", "albert")  # middle


def test_whole_word_pins_to_word_boundaries():
    assert matches("The REPORT is due", "report", whole_word=True)
    assert matches("report", "report", whole_word=True) and matches("(report)", "report", whole_word=True)
    assert not matches("Reportedly late", "report", whole_word=True)
    assert not matches("a subreport", "report", whole_word=True)


def test_case_sensitive_flag():
    assert build_regex(SearchOptions(terms=("x",), case_sensitive=True))[1] == "c"
    assert build_regex(SearchOptions(terms=("x",)))[1] == "i"
    assert not matches("The REPORT", "report", case_sensitive=True)


def test_other_metacharacters_are_literal():
    assert matches("is 100% done", "100%")
    assert matches("x user_name y", "user_name") and not matches("x username y", "user_name")
    assert matches("a.b", "a.b") and not matches("axb", "a.b")
    assert matches("f(x)", "f(x)") and matches("[a]", "[a]") and matches("a|b", "a|b")


def test_wildcards_are_always_on():
    assert matches("colour", "colo?r") and not matches("color", "colo?r")
    assert matches("color", "colo*r") and matches("colour", "colo*r")
    assert matches("colorado", "colo*r")  # default contains: may run on


def test_wildcards_work_with_whole_word():
    assert matches("colour", "colo*r", whole_word=True)
    assert not matches("colorado", "colo*r", whole_word=True)
    assert matches("albertson", "albert*", whole_word=True)
    assert not matches("talbert", "albert*", whole_word=True)


def test_star_does_not_cross_word_boundaries():
    assert not matches("colo r", "colo*r") and not matches("colo, r", "colo*r")


def test_escaped_wildcards_and_backslash_are_literal():
    assert matches("a colo?r b", r"colo\?r") and not matches("a colour b", r"colo\?r")
    assert matches("2*3", r"2\*3") and not matches("2x3", r"2\*3")
    assert matches(r"a\b", r"a\\b")
    assert matches("trailing\\", "trailing\\")  # a lone backslash is just a backslash


def test_phrase_requires_adjacent_words_across_punctuation():
    assert matches("the quick, brown fox", "quick brown fox")
    assert matches("the quick, brown fox", "quick", "brown", "fox")  # several terms = one phrase
    assert not matches("quick red brown fox", "quick brown fox")
    assert not matches("quickbrown fox", "quick brown fox")


def test_phrase_contains_extends_first_and_last_word_only():
    assert not matches("quick xbrown fox", "quick brown fox")  # inner words stay whole
    assert matches("quick brown foxes", "quick brown fox")
    assert matches("a superquick brown fox", "quick brown fox")
    assert not matches("quickly brown fox", "quick brown fox")  # 'quickly' is not 'quick'
    assert not matches("quick brown foxes", "quick brown fox", whole_word=True)


def test_near_distance_and_order():
    assert matches("a quick and lazy day", "quick", "lazy", near=2)
    assert matches("a lazy and quick day", "quick", "lazy", near=2)  # either order
    assert not matches("a quick and lazy day", "quick", "lazy", near=1)
    assert matches("quick lazy", "quick", "lazy", near=1)
    assert not matches("quickly lazy", "quick", "lazy", near=3, whole_word=True)


def test_near_contains_by_default():
    assert matches("Albert met Mr Albertson", "alb", "mr", near=3)
    assert not matches("Albert met Mr Albertson", "alb", "mr", near=3, whole_word=True)


@pytest.mark.parametrize("terms,kw", [
    ((), {}),
    (("   ",), {}),
    (("one",), dict(near=3)),
    (("a", "b", "c"), dict(near=3)),
    (("a b", "c"), dict(near=3)),
    (("a", "b"), dict(near=0)),
    (("a", "b"), dict(near=256)),
    (("x" * 600,), {}),
])
def test_invalid_options_raise(terms, kw):
    with pytest.raises(ValueError):
        build_regex(SearchOptions(terms=terms, **kw))
