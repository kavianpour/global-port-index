"""Command-line interface: ``python -m port_index ...``"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .build import build_database, fetch_anchorages
from .index import PortIndex, default_db_path


def _cmd_build(args: argparse.Namespace) -> int:
    anchorages = None
    if args.anchorages:
        bbox = tuple(args.anchorage_bbox) if args.anchorage_bbox else None
        print("querying OpenStreetMap Overpass for anchorages ...")
        anchorages = fetch_anchorages(bbox)
        print(f"  {len(anchorages)} anchorages")

    meta = build_database(
        Path(args.out),
        wpi_csv=Path(args.wpi) if args.wpi else None,
        anchorages=anchorages,
    )
    print(json.dumps(meta["counts"], indent=2))
    print(f"wrote {args.out}")
    return 0


def _cmd_resolve(args: argparse.Namespace) -> int:
    with PortIndex(args.db) as idx:
        record = idx.resolve(args.locode)
    if record is None:
        print(f"{args.locode}: not found", file=sys.stderr)
        return 1
    print(json.dumps(record._asdict(), indent=2))
    return 0


def _cmd_nearest(args: argparse.Namespace) -> int:
    with PortIndex(args.db) as idx:
        record = idx.nearest(args.lat, args.lon, max_nm=args.max_nm)
    if record is None:
        print("no port within range", file=sys.stderr)
        return 1
    print(json.dumps(record._asdict(), indent=2))
    return 0


def _cmd_search(args: argparse.Namespace) -> int:
    with PortIndex(args.db) as idx:
        for record in idx.search(args.name, limit=args.limit):
            print(f"{record.locode}  {record.name}  ({record.country})")
    return 0


def _cmd_stats(args: argparse.Namespace) -> int:
    with PortIndex(args.db) as idx:
        print(json.dumps(idx.stats(), indent=2))
    return 0


def _allow_piping() -> None:
    """Exit quietly when output is piped into a command that closes early.

    Without this, `... | head` raises BrokenPipeError and prints a traceback,
    which is noise rather than information.
    """
    try:
        import signal

        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except (ImportError, AttributeError, ValueError):  # e.g. Windows
        pass

def main(argv=None) -> int:
    _allow_piping()
    parser = argparse.ArgumentParser(
        prog="port_index",
        description="Build and query a global port/anchorage index.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_build = sub.add_parser("build", help="download sources and write the SQLite DB")
    p_build.add_argument("--out", default=str(default_db_path()))
    p_build.add_argument("--wpi", help="path to the NGA World Port Index CSV")
    p_build.add_argument(
        "--anchorages", action="store_true", help="also query OSM for anchorages"
    )
    p_build.add_argument(
        "--anchorage-bbox",
        nargs=4,
        type=float,
        metavar=("MIN_LON", "MIN_LAT", "MAX_LON", "MAX_LAT"),
    )
    p_build.set_defaults(func=_cmd_build)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=str(default_db_path()))

    p_resolve = sub.add_parser("resolve", parents=[common], help="look up a LOCODE")
    p_resolve.add_argument("locode")
    p_resolve.set_defaults(func=_cmd_resolve)

    p_nearest = sub.add_parser("nearest", parents=[common], help="nearest port")
    p_nearest.add_argument("lat", type=float)
    p_nearest.add_argument("lon", type=float)
    p_nearest.add_argument("--max-nm", type=float, default=200.0, dest="max_nm")
    p_nearest.set_defaults(func=_cmd_nearest)

    p_search = sub.add_parser("search", parents=[common], help="search by name")
    p_search.add_argument("name")
    p_search.add_argument("--limit", type=int, default=20)
    p_search.set_defaults(func=_cmd_search)

    p_stats = sub.add_parser("stats", parents=[common], help="dataset statistics")
    p_stats.set_defaults(func=_cmd_stats)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
