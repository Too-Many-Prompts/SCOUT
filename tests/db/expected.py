"""Ground truth for tests/db/seed.sql. Keep in sync with the seed.

Each entry is an (owner, table, column, value) key as produced by Match.key.
Semantics assumed by these expectations (the contract the implementation must meet):
  - Default: a term may match inside longer words ('albert' finds Talbert).  whole_word=True
    restricts to whole words.  Several terms are a phrase: adjacent, in order, across punctuation.
  - Matching is case-insensitive unless case_sensitive is set.
  - '*' (zero or more word characters) and '?' (exactly one) are always wildcards; '\\*' '\\?'
    are literals.  '%' '_' and other regex characters are always literal.
  - near=N: two words within N word positions of each other, either order.
  - Views and non-string columns are never searched.
"""
SCHEMAS = ("SCOUT_HR", "SCOUT_DOCS", "SCOUT_ODD", "SCOUT_HIDDEN")

EMP_NOTES = ("SCOUT_HR", "EMPLOYEES", "NOTES")
DOC_BODY = ("SCOUT_DOCS", "DOCUMENTS", "BODY")
DOC_TITLE = ("SCOUT_DOCS", "DOCUMENTS", "TITLE")
DOC_AUTHOR = ("SCOUT_DOCS", "DOCUMENTS", "AUTHOR")
WEIRD = ("SCOUT_ODD", "MixedCase Table", "Weird Col")
SECRET = ("SCOUT_HIDDEN", "SECRETS", "NOTE")


def key(col, value):
    return (*col, value)


REPORT_NOTES_CI = {
    key(EMP_NOTES, "Handles the quarterly audit report"),
    key(EMP_NOTES, "Prefers remote work; the report is due Friday"),
    key(EMP_NOTES, "REPORT in uppercase for case tests"),
}
REPORT_NOTES_CS = REPORT_NOTES_CI - {key(EMP_NOTES, "REPORT in uppercase for case tests")}
REPORT_NOTES_CONTAINS_CI = REPORT_NOTES_CI | {key(EMP_NOTES, "Reportedly late on deliverables")}  # not whole-word
REPORT_SECRET = {key(SECRET, "confidential audit report")}  # only with DBA tier / privileges

FOX_BODY = key(DOC_BODY, "The quick brown fox jumps over the lazy dog")
FOX_TITLE = key(DOC_TITLE, "The QUICK brown FOX club")
PHRASE_FOX_CI = {FOX_BODY, FOX_TITLE}
PHRASE_FOX_CS = {FOX_BODY}

QUICK_LAZY_2 = key(DOC_BODY, "A quick and lazy afternoon")  # distance 2
QUICK_LAZY_6 = FOX_BODY  # distance 6

COLOUR = key(WEIRD, "colour")
COLOR = key(WEIRD, "color")
COLORADO = key(WEIRD, "Colorado")
COLORS_SENTENCE = key(WEIRD, "The colors are bright")

PERCENT_ROW = key(EMP_NOTES, "Said 100% done; check user_name field")
QUOTED_ROW = key(WEIRD, "it's a 'quoted' value")
AUTHOR_MULLER = key(DOC_AUTHOR, "Zoë Müller")

ACTIVE_STATUS_ROWS = 8  # SCOUT_HR.EMPLOYEES.STATUS is CHAR(10), padded; count only

ALBERT_WHOLE = key(EMP_NOTES, "Albert met Mr Albertson")  # whole word 'Albert'
ALBERTINA = key(EMP_NOTES, "Albertina")  # prefix-only
TALBERT = key(EMP_NOTES, "Gilbert and Talbert")  # substring-only
ALBERT_WORD_ONLY = {ALBERT_WHOLE}  # whole_word=True
ALBERT_PREFIX = {ALBERT_WHOLE, ALBERTINA}  # whole_word=True with 'albert*'
ALBERT_SUBSTRING = ALBERT_PREFIX | {TALBERT}  # the default (contains)
EMPLOYEE_ROWS = 10

INDEXED_BODY = ("SCOUT_DOCS", "INDEXED_NOTES", "BODY")
PLAIN_BODY = ("SCOUT_DOCS", "PLAIN_NOTES", "BODY")
ZEBRA_TEXTS = {"A zebra crossing ahead"}  # whole word 'zebra'
ZEBRA_CONTAINS_TEXTS = ZEBRA_TEXTS | {"Zebras graze at dawn"}  # default (contains)

BROKEN = ("SCOUT_BROKEN", "EXT_MISSING", "TXT")  # visible in the catalog, fails when queried

LATE_HIT = ("SCOUT_ODD", "LATE_HIT", "TXT")  # single hit 'lateneedle', in the last of 200 rows
