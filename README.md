# global-port-index

[![Open in Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/kavianpour/global-port-index/blob/main/notebooks/quickstart.ipynb)

A queryable index of every seaport and anchorage in the public record, built
from UN/LOCODE, the NGA World Port Index and OpenStreetMap.

Port data is scattered across three incompatible public sources. UN/LOCODE has
universal coverage and poor coordinates. The World Port Index has excellent
harbour attributes and covers 3,824 ports. OpenStreetMap has anchorages and
nothing else. Joining them correctly — without inventing matches that do not
exist — is the work this package does.

```python
from port_index import PortIndex

idx = PortIndex("data/ports.sqlite")

idx.resolve("NLRTM")
# PortRecord(locode='NLRTM', name='Rotterdam', country='NL',
#            lat=51.91667, lon=4.5, is_seaport=True,
#            harbor_size='Large', harbor_type='River (Basins)',
#            anchorage_depth_m=11.0, max_draft_m=None,
#            source='unlocode+improved:UN/LOCODE')

idx.nearest(51.98, 3.60)              # nearest seaport to a position
idx.in_bbox((103.6, 1.15, 104.1, 1.35))   # everything in Singapore Strait
idx.nearest_anchorage(51.98, 3.95)    # nearest charted anchorage
idx.major_ports()                     # WPI Large/Medium seaports only
```

## Measured: coverage, joins, and query cost

![World ports](docs/figures/figure_1_world_ports.png)

Real numbers from a full build, not projections:

- **Coordinate coverage: 80.2% → 98.7%** by overlaying a second public source
  onto the canonical UN/LOCODE list.
- **21% of resolved coordinates trace back to OpenStreetMap**, not an official
  maritime authority — exposed per-row via `PortRecord.source`, not buried in
  a build log.
- **WPI join: 87.2% exact-LOCODE, 0.2% spatial fallback, 12.6% left
  unmatched** — the fallback is deliberately conservative rather than
  maximised.
- **`nearest()` is 27.4x faster than a brute-force scan** across 17,520
  seaports, and the gap widens as the table grows.
- One Large-harbor marker sits alone in the South Atlantic. It isn't a bug —
  it's Jamestown, Saint Helena, correctly placed.

Full write-up, reproducible in one command: **[docs/ANALYSIS.md](docs/ANALYSIS.md)**

## Install and build

```bash
pip install git+https://github.com/kavianpour/global-port-index
python -m port_index build --out data/ports.sqlite
```

The database is built, not shipped, so you always know exactly which upstream
revision you have. A full build takes a few minutes.

Optional enrichment:

```bash
# harbour size, type, anchorage depth, max draft
python -m port_index build --wpi UpdatedPub150.csv --out data/ports.sqlite

# plus anchorages from OpenStreetMap for one region
python -m port_index build --wpi UpdatedPub150.csv --anchorages \
    --anchorage-bbox 3.0 51.0 5.5 53.0 --out data/ports.sqlite
```

Download the World Port Index (Pub 150) CSV once from the NGA Maritime Safety
Information site. It has no stable machine-readable URL, which is why it is a
local file argument rather than a download.

Runtime dependencies: **none**. Standard library only.

## What a build actually produces

Measured on a real build:

| Metric | Value |
|---|---|
| Ports | 116,067 |
| Seaports | 17,520 |
| With coordinates | 114,575 (98.7%) |
| WPI-enriched | 3,245 |
| Exact LOCODE matches | 3,333 of 3,824 WPI rows |
| Spatial fallback matches | 9 |

## How the WPI join works, and why it is conservative

WPI rows are matched to the UN/LOCODE spine in two passes:

1. **Exact LOCODE join.** Deterministic, essentially zero false positives.
   Covers about 87% of WPI rows.
2. **Spatial + name fallback**, for rows whose LOCODE is blank, malformed, or
   absent from the spine. The candidate must be the nearest seaport within a
   small radius **and** its normalised name must agree exactly. Accents,
   punctuation and case are stripped before comparison.

Anything ambiguous is left unmatched. That asymmetry is deliberate: a missing
`harbor_size` is a gap you can see and work around, whereas a wrong one is
invisible and will be trusted. The fallback recovers real terminal-versus-parent
cases without manufacturing plausible-looking nonsense — it contributed 9
matches out of 482 residual rows, and it should be a small number.

Numeric sentinels are normalised on the way in. WPI uses `0.0` to mean "not
recorded", which is indistinguishable from a genuine zero depth once it is in
your model; it is stored as `NULL`. So are `"Unknown"` and blank categoricals.

## Coordinates

The canonical UN/LOCODE list stores coordinates in a compact format
(`5155N 00430E`) at one-arcminute resolution, and leaves about 20% of rows
blank. The build overlays decimal coordinates from the
`improved-un-locodes` mirror where available, which is what lifts coverage from
80.2% to 98.7%. Each record's `source` field records which path produced its
position, so you can filter on provenance rather than guessing.

## Concurrency

`PortIndex` is safe to share across threads.

This is worth a sentence because the obvious implementation is not. A single
module-level `sqlite3` connection shared by several threads fails
*intermittently*: concurrent cursors raise `InterfaceError` under load, and —
much worse — can return silently truncated rows, which looks like a data
problem rather than a threading problem and costs a day to find. Each thread
here caches its own read-only connection, and `check_same_thread` is left at its
default so any accidental cross-thread reuse fails loudly instead of corrupting
a read.

Verified with 8 threads × 60 interleaved queries.

Connections are opened `mode=ro`. Calling code cannot damage the artifact.

## Command line

```bash
port-index stats   --db data/ports.sqlite
port-index resolve NLRTM --db data/ports.sqlite
port-index nearest 51.98 3.60 --db data/ports.sqlite
port-index search  "singapore" --db data/ports.sqlite
```

## API

| Method | Purpose |
|---|---|
| `resolve(locode)` | Exact lookup; case- and space-insensitive |
| `search(name, limit)` | Substring search on port name |
| `nearest(lat, lon, max_nm, seaport_only)` | Nearest port, bounded widening search |
| `in_bbox(bbox)` | R\*Tree-indexed spatial query |
| `major_ports(harbor_sizes, require_coords)` | WPI Large/Medium seaports |
| `anchorages_in_bbox(bbox)` | Anchorages in a window |
| `nearest_anchorage(lat, lon, max_nm)` | Nearest charted anchorage |
| `stats()` | Row counts and coverage, for checking a build |

`nearest()` widens its search box geometrically until it finds candidates, then
computes exact great-circle distances over that small set. It is a bounded
spatial query, not a graph search.

## Provenance

Every build writes a `.meta.json` sidecar with the source URLs, byte counts,
SHA-256 of each input, WPI match statistics, and the SHA-256 of the database
itself. Two people can confirm they are working from the same data.

## Data licences

- UN/LOCODE — UNECE, freely redistributable
- World Port Index (Pub 150) — NGA, US Government work, public domain
- OpenStreetMap — © OpenStreetMap contributors, ODbL. If you redistribute
  anchorage data you must attribute and share alike.

This package's code is MIT. The data it downloads is not, and the terms above
travel with it.

**Not for navigation.** Port coordinates are reference points, not berth
positions, and WPI depths are advisory.

## Licence

MIT
