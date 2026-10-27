"""How a hit is shown: the snippet with … where Oracle cut the value; whitespace folded for one-line views."""
from scout.display import parts, text
from scout.search import Match, Snippet


def snip(before, hit, after, cut_before=False, cut_after=False, more=False):
    return Match("O", "T", "C", before + hit + after, "r", Snippet(before, hit, after, cut_before, cut_after, more))


def test_parts_mark_cut_sides_and_fold_whitespace():
    m = snip("ab \n\n ", "zq", " cd", cut_before=True)
    assert parts(m, fold=True) == ("…ab ", "zq", " cd")
    assert text(m, fold=False) == "…ab \n\n zq cd"


def test_cut_after_puts_the_marker_at_the_end():
    assert text(snip("", "zq", " cd", cut_after=True), fold=True) == "zq cd…"


def test_whitespace_inside_the_hit_folds_too():
    assert parts(snip("a ", "quick\r\n brown", " b"), fold=True) == ("a ", "quick brown", " b")


def test_full_value_match_has_no_markers():
    assert parts(Match("O", "T", "C", "  x \n y  "), fold=True) == ("", "x y", "")
    assert text(Match("O", "T", "C", "x \n y"), fold=False) == "x \n y"
