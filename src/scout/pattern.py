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


_WILDCARD_CHARS = set("*?%_")


def _has_real_character(token: str) -> bool:
    """True if the token contains something other than wildcard characters (* ? % _).

    An escaped wildcard (\\* or \\?) is a real, literal character."""
    i = 0
    while i < len(token):
        ch = token[i]
        if ch == "\\" and i + 1 < len(token) and token[i + 1] in "*?\\":
            return True
        if ch not in _WILDCARD_CHARS:
            return True
        i += 1
    return False


_WILDCARD_ONLY = (
    "Searching for only wildcard characters (like * or ?) would match everything; "
    "include at least one letter or digit."
)


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
        singles = [_single(t) for t in opts.terms]
        if not all(_has_real_character(t) for t in singles):
            raise ValueError(_WILDCARD_ONLY)
        a, b = (_core(t) for t in singles)
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
        if not any(_has_real_character(t) for t in tokens):
            raise ValueError(_WILDCARD_ONLY)
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


def _token_min_length(token: str) -> int:
    """Characters a single word needs at minimum: * needs none, ? needs one, an escaped
    wildcard or backslash is one literal, everything else is one."""
    n, i = 0, 0
    while i < len(token):
        if token[i] == "\\" and i + 1 < len(token) and token[i + 1] in "*?\\":
            n += 1
            i += 1
        elif token[i] != "*":
            n += 1
        i += 1
    return n


def min_match_length(opts: "SearchOptions") -> int:
    """Length in characters of the shortest text that could match: a column declared shorter
    than this cannot contain a match. Words are separated by at least one character."""
    if opts.near is not None:
        return sum(_token_min_length(_single(t)) for t in opts.terms) + 1
    tokens = " ".join(opts.terms).split()
    return sum(_token_min_length(t) for t in tokens) + max(len(tokens) - 1, 0)
