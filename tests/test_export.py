import io

from scout.export import write_csv
from scout.search import Match

def test_csv_header_and_rows_sorted():
    out = io.StringIO()
    n = write_csv([Match("B", "T", "C", "v2"), Match("A", "T", "C", "v1")], out)
    assert n == 2
    assert out.getvalue().splitlines() == ["owner,table,column,value", "A,T,C,v1", "B,T,C,v2"]


def test_csv_quotes_commas_quotes_and_newlines():
    out = io.StringIO()
    write_csv([Match("A", "T", "C", 'a,"b"\nc')], out)
    assert '"a,""b""\nc"' in out.getvalue()


def test_csv_complete_column_marks_incomplete_columns():
    out = io.StringIO()
    n = write_csv(
        [Match("A", "T", "D", "v2"), Match("A", "T", "C", "v1")], out, incomplete={("A", "T", "D")}
    )
    assert n == 2
    assert out.getvalue().splitlines() == [
        "owner,table,column,value,complete", "A,T,C,v1,yes", "A,T,D,v2,no"]


def test_csv_without_incomplete_keeps_the_four_column_format():
    out = io.StringIO()
    write_csv([Match("A", "T", "C", "v1")], out)
    assert out.getvalue().splitlines()[0] == "owner,table,column,value"
