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


_LIGATURE_GROUPS = ("ffi", "ffl", "ff", "fi", "fl", "st", "ss")  # one character can fold to each


def _token_parts(token: str) -> tuple[list[str], int]:
    """Split a word into its literal runs and the count of single-character `?` wildcards.
    `*` separates runs; an escaped wildcard or backslash is a literal character."""
    runs, current, fixed = [], [], 0
    i = 0
    while i < len(token):
        ch = token[i]
        if ch == "\\" and i + 1 < len(token) and token[i + 1] in "*?\\":
            current.append(token[i + 1])
            i += 1
        elif ch == "*":
            runs.append("".join(current))
            current = []
        elif ch == "?":
            runs.append("".join(current))
            current = []
            fixed += 1
        else:
            current.append(ch)
        i += 1
    runs.append("".join(current))
    return [r for r in runs if r], fixed


def _folded_length(run: str) -> int:
    """Fewest characters of text that can match `run` case-insensitively. Oracle folds a
    ligature or sharp s to several letters (a single character can match fi, ffi, st, ss),
    and non-ASCII text can fold up to 3 characters into one, so be conservative."""
    if not run.isascii():
        return -(-len(run) // 3)
    low = run.lower()
    best = [0] * (len(low) + 1)  # best[i] = fewest characters for low[:i]
    for i in range(1, len(low) + 1):
        best[i] = best[i - 1] + 1
        for group in _LIGATURE_GROUPS:
            if i >= len(group) and low[i - len(group):i] == group:
                best[i] = min(best[i], best[i - len(group)] + 1)
    return best[-1]


def _token_min_length(token: str, case_sensitive: bool) -> int:
    runs, fixed = _token_parts(token)
    return fixed + sum(len(r) if case_sensitive else _folded_length(r) for r in runs)


def min_match_length(opts: "SearchOptions") -> int:
    """Length in characters of the shortest text that could match: a column declared shorter
    than this cannot contain a match. Words are separated by at least one character."""
    if opts.near is not None:
        tokens = [_single(t) for t in opts.terms]
        gaps = 1
    else:
        tokens = " ".join(opts.terms).split()
        gaps = max(len(tokens) - 1, 0)
    return sum(_token_min_length(t, opts.case_sensitive) for t in tokens) + gaps


MIN_LITERAL = 3  # shorter literals are not selective enough to be worth a pre-check
_SAFE_FOLD = set("abcdefghijklmnopqrstuvwxyz0123456789") - {"s"}
_NO_ADJACENT = {("f", "f"), ("f", "i"), ("f", "l"), ("s", "t")}  # may be a single ligature character


def _safe_pieces(run: str) -> list[str]:
    """Pieces of a (lowercased) run that NLS_LOWER-based substring search can never miss when
    the regex matches case-insensitively: ASCII letters/digits except `s` (long s), and no
    letter that sits in a possible ligature pair (ff, fi, fl, st): in 'fine' the text may hold
    one character for 'fi', so neither the f nor the i is guaranteed to be present."""
    unsafe = {i for i, ch in enumerate(run) if ch not in _SAFE_FOLD}
    for i in range(len(run) - 1):
        if (run[i], run[i + 1]) in _NO_ADJACENT:
            unsafe |= {i, i + 1}
    pieces, current = [], ""
    for i, ch in enumerate(run):
        if i in unsafe:
            pieces.append(current)
            current = ""
        else:
            current += ch
    pieces.append(current)
    return pieces


def required_literal(opts: "SearchOptions") -> str | None:
    """A literal every matching text must contain, for a cheap substring pre-check before the
    regex; None if there is no selective one (at least MIN_LITERAL characters).

    Case-sensitive: the longest wildcard-free run, as typed. Case-insensitive: lowercased, and
    only pieces provably safe under Oracle's case folding (see _safe_pieces)."""
    tokens = [_single(t) for t in opts.terms] if opts.near is not None else " ".join(opts.terms).split()
    candidates: list[str] = []
    for token in tokens:
        runs, _ = _token_parts(token)
        for run in runs:
            candidates += [run] if opts.case_sensitive else _safe_pieces(run.lower())
    best = ""
    for c in candidates:
        if len(c) > len(best):
            best = c
    return best if len(best) >= MIN_LITERAL else None
