"""Turn search options into an Oracle REGEXP_LIKE pattern. Pure: no database, no UI.

The pattern is always passed to the database as a bind variable; this module only builds
the text. Oracle regex has no \\b, so word boundaries are spelled with POSIX classes.
"""
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scout.search import SearchOptions

W = "[[:alnum:]_]"  # a word character
NW = "[^[:alnum:]_]"  # a non-word character
MAX_PATTERN_BYTES = 512  # Oracle's REGEXP_LIKE pattern limit
MAX_DISTANCE = 255  # Oracle's interval {m,n} upper bound is 255; we use {0,near-1}

_REGEX_META = set("\\^$.|?*+()[]{}")


def _core(token: str) -> str:
    """One word's pattern: regex-escaped, with * and ? as wildcards and \\ escaping them."""
    out = []
    i = 0
    while i < len(token):
        ch = token[i]
        if ch == "\\" and i + 1 < len(token) and token[i + 1] in "*?\\":
            i += 1
            ch = token[i]
            out.append("\\" + ch)  # escaped wildcard or backslash: a literal
        elif ch == "*":
            out.append(f"{W}*")
        elif ch == "?":
            out.append(W)
        elif ch in _REGEX_META:
            out.append("\\" + ch)
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def build_regex(opts: "SearchOptions") -> tuple[str, str]:
    """Return (pattern, match_param) for REGEXP_LIKE. Raises ValueError on bad input.

    By default a term may match inside longer words (albert finds Talbert); whole_word pins it
    to word boundaries. * and ? are always wildcards (\\* and \\? are literals)."""
    flags = "c" if opts.case_sensitive else "i"
    contains = not opts.whole_word
    lead = "" if contains else f"(^|{NW})"
    end = "" if contains else f"({NW}|$)"

    if opts.near is not None:
        if len(opts.terms) != 2:
            raise ValueError("--near takes exactly two terms")
        if not 1 <= opts.near <= MAX_DISTANCE:
            raise ValueError(f"--near distance must be 1..{MAX_DISTANCE}")
        a, b = (_core(_single(t)) for t in opts.terms)
        run_on = f"{W}*" if contains else ""
        reach = f"{W}*" if contains else ""
        gap = f"{NW}+({W}+{NW}+){{0,{opts.near - 1}}}{reach}"
        pattern = (
            f"({lead}{a}{run_on}{gap}{b}{end}|{lead}{b}{run_on}{gap}{a}{end})"
        )
    else:
        tokens = " ".join(opts.terms).split()
        if not tokens:
            raise ValueError("empty search term")
        body = f"{NW}+".join(_core(t) for t in tokens)
        pattern = f"{lead}{body}{end}"

    if len(pattern.encode()) > MAX_PATTERN_BYTES:
        raise ValueError("search term is too long for Oracle's regular expression limit")
    return pattern, flags


def _single(term: str) -> str:
    tokens = term.split()
    if len(tokens) != 1:
        raise ValueError("each proximity term must be a single word")
    return tokens[0]
