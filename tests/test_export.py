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
