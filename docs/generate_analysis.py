"""
Reproduce every figure and table in docs/ANALYSIS.md.

Requires a built database (see README). Takes under a minute once the
database exists.

Run:  python docs/generate_analysis.py [path/to/ports.sqlite]
"""
from __future__ import annotations

import json
import random
import sqlite3
import sys
import time
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from port_index import PortIndex, haversine_nm  # noqa: E402

FIGDIR = Path(__file__).parent / "figures"
DATADIR = Path(__file__).parent / "data"
FIGDIR.mkdir(parents=True, exist_ok=True)
DATADIR.mkdir(parents=True, exist_ok=True)

DB_PATH = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/ports.sqlite")


def load_conn() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"{DB_PATH} not found. Build it first:\n"
            f"  python -m port_index build --wpi UpdatedPub150.csv --out {DB_PATH}"
        )
    return sqlite3.connect(DB_PATH)


def source_buckets(conn) -> list:
    rows = conn.execute(
        """
        SELECT
          CASE
            WHEN source = 'unlocode' THEN 'unlocode (DMS only)'
            WHEN source LIKE 'unlocode+improved:UN/LOCODE' THEN 'improved: UN/LOCODE-sourced'
            WHEN source LIKE 'unlocode+improved:Geonames%' THEN 'improved: Geonames-sourced'
            WHEN source LIKE 'unlocode+improved:%openstreetmap%' THEN 'improved: OSM-sourced'
            WHEN source LIKE 'unlocode+improved:%' THEN 'improved: other'
            ELSE 'other'
          END AS bucket, COUNT(*) c
        FROM ports GROUP BY bucket ORDER BY c DESC
        """
    ).fetchall()
    (DATADIR / "source_buckets.json").write_text(json.dumps(rows, indent=2))
    return rows


def top_countries(conn) -> list:
    rows = conn.execute(
        "SELECT country, COUNT(*) c FROM ports WHERE is_seaport=1 "
        "GROUP BY country ORDER BY c DESC LIMIT 15"
    ).fetchall()
    (DATADIR / "top_countries.json").write_text(json.dumps(rows, indent=2))
    return rows


def harbor_sizes(conn) -> list:
    rows = conn.execute(
        "SELECT harbor_size, COUNT(*) FROM ports WHERE harbor_size IS NOT NULL "
        "GROUP BY harbor_size ORDER BY 2 DESC"
    ).fetchall()
    (DATADIR / "harbor_sizes.json").write_text(json.dumps(rows, indent=2))
    return rows


def harbor_types(conn) -> list:
    rows = conn.execute(
        "SELECT harbor_type, COUNT(*) FROM ports WHERE harbor_type IS NOT NULL "
        "GROUP BY harbor_type ORDER BY 2 DESC LIMIT 10"
    ).fetchall()
    (DATADIR / "harbor_types.json").write_text(json.dumps(rows, indent=2))
    return rows


def query_benchmark(conn) -> dict:
    """R-tree indexed nearest() vs. brute-force full-table distance scan."""
    idx = PortIndex(DB_PATH)
    random.seed(42)
    seaports = conn.execute(
        "SELECT lat, lon FROM ports WHERE is_seaport=1 AND lat IS NOT NULL"
    ).fetchall()
    sample = random.sample(seaports, 200)

    t0 = time.time()
    for lat, lon in sample:
        idx.nearest(lat + random.uniform(-2, 2), lon + random.uniform(-2, 2))
    t_rtree = time.time() - t0

    def brute_nearest(lat, lon):
        best = None
        for r in conn.execute(
            "SELECT lat, lon FROM ports WHERE is_seaport=1 AND lat IS NOT NULL"
        ):
            d = haversine_nm(lat, lon, r[0], r[1])
            if best is None or d < best:
                best = d
        return best

    t0 = time.time()
    for lat, lon in sample[:20]:
        brute_nearest(lat + random.uniform(-2, 2), lon + random.uniform(-2, 2))
    t_brute_20 = time.time() - t0
    t_brute_est = t_brute_20 / 20 * 200

    result = {
        "rtree_200_queries_s": round(t_rtree, 4),
        "rtree_ms_per_query": round(t_rtree / 200 * 1000, 3),
        "brute_20_queries_s": round(t_brute_20, 4),
        "brute_ms_per_query": round(t_brute_20 / 20 * 1000, 3),
        "brute_extrapolated_200_s": round(t_brute_est, 3),
        "speedup_factor": round(t_brute_est / t_rtree, 1),
    }
    (DATADIR / "query_benchmark.json").write_text(json.dumps(result, indent=2))
    idx.close()
    return result


def figure_world_ports(conn):
    import obstacle_builder as ob  # optional cross-package dependency for the figure only

    land = ob.fetch_land(bbox=(-180, -90, 180, 90), resolution="50m")
    major = conn.execute(
        "SELECT lat, lon, harbor_size FROM ports "
        "WHERE harbor_size IN ('Large','Medium') AND lat IS NOT NULL"
    ).fetchall()

    fig, ax = plt.subplots(figsize=(14, 7))
    for p in (land.geoms if hasattr(land, "geoms") else [land]):
        xs, ys = p.exterior.xy
        ax.fill(xs, ys, fc="#d8cbb0", ec="#a0917a", lw=0.2)

    large = [(lo, la) for la, lo, sz in major if sz == "Large"]
    medium = [(lo, la) for la, lo, sz in major if sz == "Medium"]
    ax.scatter(*zip(*medium), s=6, c="#2980b9", alpha=0.6, label=f"Medium ({len(medium)})")
    ax.scatter(*zip(*large), s=16, c="#c0392b", alpha=0.85, label=f"Large ({len(large)})")

    ax.set_xlim(-180, 180)
    ax.set_ylim(-60, 80)
    ax.set_aspect("equal")
    ax.legend(loc="lower left", fontsize=10, framealpha=0.9)
    ax.set_title(
        "World Port Index Large/Medium seaports over 50m coastline "
        f"({len(large) + len(medium)} ports total)",
        fontsize=12,
    )
    plt.tight_layout()
    plt.savefig(FIGDIR / "figure_1_world_ports.png", dpi=150)
    plt.close(fig)


def figure_distributions(countries, sizes, types):
    fig, axes = plt.subplots(1, 3, figsize=(16, 5))

    ax = axes[0]
    names = [c[0] for c in countries][::-1]
    counts = [c[1] for c in countries][::-1]
    ax.barh(names, counts, color="#2980b9")
    ax.set_title("Top 15 countries by seaport count")
    ax.set_xlabel("seaports")

    ax = axes[1]
    order = ["Very Small", "Small", "Medium", "Large"]
    size_map = dict(sizes)
    vals = [size_map.get(s, 0) for s in order]
    ax.bar(order, vals, color=["#95a5a6", "#3498db", "#e67e22", "#c0392b"])
    ax.set_title("WPI harbor size distribution")
    ax.set_ylabel("count")
    for i, v in enumerate(vals):
        ax.text(i, v, str(v), ha="center", va="bottom", fontsize=9)

    ax = axes[2]
    tnames = [t[0] for t in types][::-1]
    tcounts = [t[1] for t in types][::-1]
    ax.barh(tnames, tcounts, color="#16a085")
    ax.set_title("Top harbor types (WPI)")
    ax.set_xlabel("count")

    plt.tight_layout()
    plt.savefig(FIGDIR / "figure_2_distributions.png", dpi=150)
    plt.close(fig)


def figure_query_benchmark(qb):
    fig, ax = plt.subplots(figsize=(6, 5))
    labels = ["R-tree\nnearest()", "Brute-force\nfull scan"]
    values = [qb["rtree_ms_per_query"], qb["brute_ms_per_query"]]
    bars = ax.bar(labels, values, color=["#2980b9", "#c0392b"])
    ax.set_ylabel("ms per query")
    ax.set_title(
        f"Nearest-seaport query cost, 17,520 seaports\n({qb['speedup_factor']}x speedup)"
    )
    for b, v in zip(bars, values):
        ax.text(b.get_x() + b.get_width() / 2, v, f"{v:.2f} ms", ha="center", va="bottom")
    plt.tight_layout()
    plt.savefig(FIGDIR / "figure_3_query_benchmark.png", dpi=150)
    plt.close(fig)


def main():
    conn = load_conn()
    countries = top_countries(conn)
    sizes = harbor_sizes(conn)
    types = harbor_types(conn)
    buckets = source_buckets(conn)
    qb = query_benchmark(conn)

    print("top countries:", countries[:5], "...")
    print("harbor sizes:", sizes)
    print("source buckets:", buckets)
    print("query benchmark:", qb)

    figure_distributions(countries, sizes, types)
    figure_query_benchmark(qb)
    try:
        figure_world_ports(conn)
    except ImportError:
        print("skipping world-map figure: pip install maritime-obstacle-builder for it")

    print("\nDone. Figures in docs/figures/, raw data in docs/data/.")


if __name__ == "__main__":
    main()
