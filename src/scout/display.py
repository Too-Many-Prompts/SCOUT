"""How a hit's value is shown: shared by the CLI, the TUI and the CSV export. Pure, no UI imports."""
import re

from scout.search import Match

CUT = "…"  # marks a side where Oracle left text out of the snippet
_SPACE = re.compile(r"\s+")


def parts(m: Match, fold: bool) -> tuple[str, str, str]:
    """(before, hit, after) with CUT where the value was cut. `fold` collapses each whitespace
    run to one space (for one-line views). A match without a snippet comes back as
    ("", value, "") with the value folded end to end, as the front ends always showed it."""
    if m.snippet is None:
        return "", " ".join(m.value.split()) if fold else m.value, ""
    s = m.snippet
    before, hit, after = (_SPACE.sub(" ", p) if fold else p for p in (s.before, s.hit, s.after))
    return (CUT if s.cut_before else "") + before, hit, after + (CUT if s.cut_after else "")


def text(m: Match, fold: bool) -> str:
    """The value as shown: the three parts joined."""
    return "".join(parts(m, fold))
