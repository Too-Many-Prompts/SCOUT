"""Time searches over the SCOUT_BENCH schema and print a comparison.

Usage: python -m tests.bench.run [--threads 8] [--limit 5] [--schema SCOUT_CLOB]
Variants: 'absent' (full scan, no hits), 'needle' (rare hits in one column), 'common' (a
word that appears in every table). Each runs unlimited and, with --limit, in locate mode.
With --schema SCOUT_CLOB (python -m tests.bench.clobs) the needle is the last word of a 100 MB CLOB.
"""
import argparse
import os
import time
from dataclasses import replace

from dotenv import load_dotenv

from scout.config import ConnectionConfig
from scout.search import SearchOptions, search
from tests.bench import clobs
from tests.bench.load import ABSENT, NEEDLE, SCHEMA

NEEDLES = {SCHEMA: NEEDLE, clobs.SCHEMA: clobs.TAIL}


def measure(cfg: ConnectionConfig, variants: dict[str, SearchOptions]) -> list[tuple[str, float, int]]:
    """Run each variant once; return (name, seconds, match_count)."""
    out = []
    for name, opts in variants.items():
        start = time.perf_counter()
        count = sum(1 for _ in search(cfg, opts))
        out.append((name, time.perf_counter() - start, count))
    return out


def default_variants(threads: int, limit: int | None, schema: str = SCHEMA) -> dict[str, SearchOptions]:
    base = SearchOptions(terms=(ABSENT,), schemas=(schema,), threads=threads)
    variants = {
        "absent": base,
        "needle": replace(base, terms=(NEEDLES[schema],)),
        "common": replace(base, terms=("the",)),
    }
    if limit is not None:
        variants.update({f"{n} (limit {limit})": replace(o, limit_per_column=limit)
                         for n, o in list(variants.items())})
    return variants


def main() -> None:
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--limit", type=int, default=None, help="also run each variant in locate mode")
    ap.add_argument("--precheck", choices=("on", "off"), default=None,
                    help="force the substring pre-check on or off (default: the library setting)")
    ap.add_argument("--single-scan", choices=("on", "off"), default=None,
                    help="force one scan per table on or off (default: the library setting)")
    ap.add_argument("--schema", choices=sorted(NEEDLES), default=SCHEMA, help="dataset to search")
    args = ap.parse_args()
    if args.single_scan:
        import scout.search

        scout.search.SINGLE_SCAN_MIN_BLOCKS = 0 if args.single_scan == "on" else None
    if args.precheck:
        import scout.search

        scout.search.PRECHECK = args.precheck == "on"
    cfg = ConnectionConfig(
        host=os.environ.get("SCOUT_TEST_DB_HOST", "localhost"),
        port=int(os.environ.get("SCOUT_TEST_DB_PORT", "1521")),
        service_name=os.environ.get("SCOUT_TEST_DB_SERVICE_NAME", "FREEPDB1"),
        user=os.environ.get("SCOUT_TEST_READER_USER", "scout_reader"),
        password=os.environ["SCOUT_TEST_READER_PASSWORD"],
    )
    print(f"{'variant':<24}{'seconds':>10}{'matches':>10}")
    for name, secs, n in measure(cfg, default_variants(args.threads, args.limit, args.schema)):
        print(f"{name:<24}{secs:>10.2f}{n:>10}")


if __name__ == "__main__":
    main()
