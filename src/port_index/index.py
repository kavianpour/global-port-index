"""
Read-only accessor over the port/anchorage SQLite artifact.

Design notes
------------
* **Stdlib only.** ``sqlite3`` ships with Python and gives indexed lookup over
  ~100k records without loading anything into memory. A pandas DataFrame would
  cost ~100 MB of RAM and a hard dependency for no benefit.
* **Read-only URI connections.** The database is opened with
  ``file:...?mode=ro``. A bug in calling code cannot corrupt the artifact.
* **Thread-local connections.** ``sqlite3`` connection objects are *not* safe
  for concurrent use. A single module-level connection shared across threads —
  the obvious implementation — fails intermittently under load: concurrent
  cursors on one connection can raise ``InterfaceError`` and, worse, can return
  silently truncated rows. Each thread therefore caches its own connection, and
  ``check_same_thread`` is left at its default so that any accidental
  cross-thread reuse fails loudly instead of corrupting a read.
"""

from __future__ import annotations

import math
import sqlite3
import threading
from pathlib import Path
from typing import List, NamedTuple, Optional, Sequence

__all__ = [
    "PortRecord",
    "AnchorageRecord",
    "PortIndex",
    "default_db_path",
]

_PORT_COLS = (
    "locode, name, country, lat, lon, is_seaport, "
    "harbor_size, harbor_type, anchorage_depth_m, max_draft_m, source"
)
_ANCH_COLS = "anchorage_id, name, lat, lon, country, source"

_EARTH_RADIUS_NM = 3440.065


class PortRecord(NamedTuple):
    """One port. ``lat``/``lon`` are ``None`` when the source had no coordinate."""

    locode: str
    name: str
    country: str
    lat: Optional[float]
    lon: Optional[float]
    is_seaport: bool
    harbor_size: Optional[str]
    harbor_type: Optional[str]
    anchorage_depth_m: Optional[float]
    max_draft_m: Optional[float]
    source: str


class AnchorageRecord(NamedTuple):
    """One charted anchorage area, reduced to a representative point."""

    anchorage_id: str
    name: str
    lat: float
    lon: float
    country: Optional[str]
    source: str


def default_db_path() -> Path:
    """Where :mod:`port_index.build` writes its artifact by default."""
    return Path.cwd() / "data" / "ports.sqlite"


def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in nautical miles on a spherical Earth.

    Accurate to roughly 0.3% against the WGS-84 ellipsoid, which is far below
    the positional uncertainty of the underlying port coordinates. Kept here so
    the package has no runtime dependency at all.
    """
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _EARTH_RADIUS_NM * math.asin(min(1.0, math.sqrt(a)))


class PortIndex:
    """Query interface over a built port/anchorage database."""

    def __init__(self, db_path=None):
        path = Path(db_path) if db_path is not None else default_db_path()
        if not path.exists():
            raise FileNotFoundError(
                f"Port database not found at {path}.\n"
                f"Build it with:  python -m port_index build --out {path}"
            )
        self._path = str(path)
        self._local = threading.local()

    # -- connection handling -------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        cache = getattr(self._local, "conn", None)
        if cache is None:
            cache = sqlite3.connect(f"file:{self._path}?mode=ro", uri=True)
            self._local.conn = cache
        return cache

    def close(self) -> None:
        """Close this thread's connection, if any."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def __enter__(self) -> "PortIndex":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- ports ---------------------------------------------------------------

    def resolve(self, locode: str) -> Optional[PortRecord]:
        """Look up a port by 5-character UN/LOCODE, case- and space-insensitive.

        ``"nl rtm"``, ``" NLRTM "`` and ``"NLRTM"`` all resolve to the same row.
        """
        key = (locode or "").strip().upper().replace(" ", "")
        row = self._conn().execute(
            f"SELECT {_PORT_COLS} FROM ports WHERE locode = ?", (key,)
        ).fetchone()
        return PortRecord(*row[:5], bool(row[5]), *row[6:]) if row else None

    def search(self, name: str, limit: int = 20) -> List[PortRecord]:
        """Case-insensitive substring search on port name."""
        pattern = f"%{(name or '').strip().lower()}%"
        rows = self._conn().execute(
            f"SELECT {_PORT_COLS} FROM ports WHERE LOWER(name) LIKE ? "
            f"ORDER BY (harbor_size IS NULL), name LIMIT ?",
            (pattern, int(limit)),
        ).fetchall()
        return [PortRecord(*r[:5], bool(r[5]), *r[6:]) for r in rows]

    def in_bbox(self, bbox: Sequence[float]) -> List[PortRecord]:
        """All ports inside ``bbox = (min_lon, min_lat, max_lon, max_lat)``.

        Backed by an R*Tree index, so this stays fast on the full dataset.
        """
        min_lon, min_lat, max_lon, max_lat = bbox
        conn = self._conn()
        ids = [
            r[0]
            for r in conn.execute(
                "SELECT id FROM ports_rtree WHERE max_lon>=? AND min_lon<=? "
                "AND max_lat>=? AND min_lat<=?",
                (min_lon, max_lon, min_lat, max_lat),
            ).fetchall()
        ]
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT {_PORT_COLS} FROM ports WHERE rowid IN ({placeholders})", ids
        ).fetchall()
        return [PortRecord(*r[:5], bool(r[5]), *r[6:]) for r in rows]

    def nearest(
        self,
        lat: float,
        lon: float,
        *,
        max_nm: float = 200.0,
        seaport_only: bool = True,
    ) -> Optional[PortRecord]:
        """Nearest port to a position, or ``None`` if none within ``max_nm``.

        The search widens the bounding box geometrically (1, 2, 4, ... degrees)
        until a candidate is found or ``max_nm`` is exceeded, then computes exact
        great-circle distances only over that small candidate set. This is a
        bounded spatial query, not a graph search.
        """
        span = 1.0
        max_span = max(1.0, max_nm / 60.0)
        while span <= max_span * 2:
            window = (lon - span, lat - span, lon + span, lat + span)
            candidates = self.in_bbox(window)
            if seaport_only:
                candidates = [c for c in candidates if c.is_seaport]
            candidates = [c for c in candidates if c.lat is not None]
            if candidates:
                best = min(
                    candidates, key=lambda p: haversine_nm(lat, lon, p.lat, p.lon)
                )
                if haversine_nm(lat, lon, best.lat, best.lon) <= max_nm:
                    return best
            span *= 2
        return None

    def major_ports(
        self,
        *,
        harbor_sizes: Sequence[str] = ("Large", "Medium"),
        require_coords: bool = True,
    ) -> List[PortRecord]:
        """Ports classified Large/Medium by the World Port Index.

        Useful for building a tractable dropdown: the full index has six figures
        of rows, of which only a few hundred are places a deep-sea vessel calls.
        """
        if not harbor_sizes:
            return []
        placeholders = ",".join("?" * len(harbor_sizes))
        sql = (
            f"SELECT {_PORT_COLS} FROM ports "
            f"WHERE harbor_size IN ({placeholders}) AND is_seaport = 1"
        )
        if require_coords:
            sql += " AND lat IS NOT NULL"
        sql += " ORDER BY country, name"
        rows = self._conn().execute(sql, list(harbor_sizes)).fetchall()
        return [PortRecord(*r[:5], bool(r[5]), *r[6:]) for r in rows]

    # -- anchorages ----------------------------------------------------------

    def anchorages_in_bbox(self, bbox: Sequence[float]) -> List[AnchorageRecord]:
        """All anchorages inside ``bbox = (min_lon, min_lat, max_lon, max_lat)``."""
        min_lon, min_lat, max_lon, max_lat = bbox
        rows = self._conn().execute(
            f"SELECT {_ANCH_COLS} FROM anchorages "
            "WHERE lon BETWEEN ? AND ? AND lat BETWEEN ? AND ?",
            (min_lon, max_lon, min_lat, max_lat),
        ).fetchall()
        return [AnchorageRecord(*r) for r in rows]

    def nearest_anchorage(
        self, lat: float, lon: float, *, max_nm: float = 50.0
    ) -> Optional[AnchorageRecord]:
        """Nearest charted anchorage, or ``None`` within ``max_nm``."""
        span = max_nm / 60.0
        candidates = self.anchorages_in_bbox(
            (lon - span, lat - span, lon + span, lat + span)
        )
        if not candidates:
            return None
        best = min(candidates, key=lambda a: haversine_nm(lat, lon, a.lat, a.lon))
        return best if haversine_nm(lat, lon, best.lat, best.lon) <= max_nm else None

    # -- misc ----------------------------------------------------------------

    def stats(self) -> dict:
        """Row counts and coordinate coverage, for sanity-checking a build."""
        conn = self._conn()
        one = lambda q: conn.execute(q).fetchone()[0]  # noqa: E731
        total = one("SELECT COUNT(*) FROM ports")
        return {
            "ports": total,
            "seaports": one("SELECT COUNT(*) FROM ports WHERE is_seaport = 1"),
            "with_coords": one("SELECT COUNT(*) FROM ports WHERE lat IS NOT NULL"),
            "wpi_enriched": one(
                "SELECT COUNT(*) FROM ports WHERE harbor_size IS NOT NULL"
            ),
            "anchorages": one("SELECT COUNT(*) FROM anchorages"),
            "coord_coverage_pct": round(
                100.0 * one("SELECT COUNT(*) FROM ports WHERE lat IS NOT NULL") / total,
                3,
            )
            if total
            else 0.0,
        }
