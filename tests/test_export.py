import csv
import io

from scout.export import write_csv
from scout.search import Match, Snippet

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


def test_csv_with_snippets_adds_more_in_value_after_value():
    out = io.StringIO()
    snip = Snippet("ab ", "zq", " cd", True, True, True)
    write_csv([Match("A", "T", "C", "ab zq cd", "r", snip)], out)
    rows = list(csv.reader(io.StringIO(out.getvalue())))
    assert rows[0] == ["owner", "table", "column", "value", "more_in_value", "sql_query"]
    assert rows[1][3:5] == ["…ab zq cd…", "yes"]


def test_csv_with_snippets_and_complete_column():
    out = io.StringIO()
    snip = Snippet("", "zq", "", False, False, False)
    write_csv([Match("A", "T", "C", "zq", "r", snip)], out, incomplete={("A", "T", "C")})
    assert out.getvalue().splitlines() == [
        "owner,table,column,value,more_in_value,complete,sql_query",
        'A,T,C,zq,no,no,"SELECT * FROM ""A"".""T"" WHERE ROWID = \'r\';"']


def test_csv_sql_query_column_pulls_back_the_whole_row():
    out = io.StringIO()
    write_csv([Match("A", "T", "C", "v1", "AAAB+/09")], out)
    rows = list(csv.reader(io.StringIO(out.getvalue())))
    assert rows[0] == ["owner", "table", "column", "value", "sql_query"]
    assert rows[1][4] == """SELECT * FROM "A"."T" WHERE ROWID = 'AAAB+/09';"""


def test_csv_sql_query_escapes_quotes_and_blanks_missing_rowids():
    out = io.StringIO()
    write_csv([Match('O"X', "T", "C", "v1", "a'b"), Match("P", "T", "C", "v2")], out)
    rows = list(csv.reader(io.StringIO(out.getvalue())))
    assert rows[1][4] == """SELECT * FROM "O""X"."T" WHERE ROWID = 'a''b';"""
    assert rows[2][4] == ""
