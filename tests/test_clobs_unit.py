"""The large-CLOB loader puts each planted word exactly where it promises (pure, no database)."""
import pytest

from tests.bench.clobs import (
    FOURK, KANA, KANA_CHAR_OFFSET, KANA_RUN, TAIL, THIRTYTWOK, Planted, books_with_huge,
    char_index_at_byte, insert_after_kana, insert_spanning_byte, plant_ja, plant_simple,
)


def byte_span(text: str, word: str) -> tuple[int, int]:
    encoded = text.encode()
    start = encoded.index(word.encode())
    return start, start + len(word.encode())


def test_char_index_at_byte_counts_utf8_bytes():
    assert char_index_at_byte("abc", 1) == 1
    assert char_index_at_byte("かかか", 3) == 1   # 3 bytes per kana
    assert char_index_at_byte("かかか", 4) == 2   # next character boundary after byte 4
    assert char_index_at_byte("ab", 10) == 2


@pytest.mark.parametrize("filler", ["a", "é", "か", "😀"])  # 1, 2, 3 and 4 bytes per character
@pytest.mark.parametrize("offset", [4_000, 32_767])
def test_word_spans_the_byte_offset(filler, offset):
    text = insert_spanning_byte(filler * 40_000, THIRTYTWOK, offset)
    start, end = byte_span(text, THIRTYTWOK)
    assert start <= offset < end


def test_kana_word_sits_at_a_character_offset_after_kana():
    text = insert_after_kana("か" * 30_000, KANA, KANA_CHAR_OFFSET)
    index = text.index(KANA)
    assert index == KANA_CHAR_OFFSET + len(KANA_RUN)
    assert text[index - len(KANA_RUN):index] == KANA_RUN


def test_plant_simple_marks_the_first_long_enough_rows():
    rows = [(1, "short", "x" * 100), (2, "mid", "x" * 9_000), (3, "long", "x" * 50_000)]
    planted: list[Planted] = []
    out = list(plant_simple(rows, planted))
    assert planted == [Planted(FOURK, "WIKI_SIMPLE", 2), Planted(THIRTYTWOK, "WIKI_SIMPLE", 3)]
    assert out[0] == rows[0]
    assert FOURK in out[1][2] and THIRTYTWOK in out[2][2]


def test_plant_refuses_when_no_row_is_long_enough():
    with pytest.raises(ValueError, match="load more rows"):
        list(plant_ja([(1, "t", "か" * 10)], []))


def test_huge_repeats_the_source_and_ends_with_the_tail_word():
    rows = books_with_huge({"A": "aaa", "B": "bbb"}, "wp", copies=3)
    assert [r[:2] for r in rows] == [(1, "A"), (2, "B"), (3, "HUGE")]
    assert rows[-1][2] == f"wp\nwp\nwp\n{TAIL}"
