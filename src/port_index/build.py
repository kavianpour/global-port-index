"""
Build the port/anchorage SQLite artifact from public sources.

Sources
-------
1. **UN/LOCODE** — the spine. Every trade location in the world gets a
   five-character code. Downloaded from a pinned commit of the
   ``datasets/un-locode`` mirror. Coordinates in the canonical list are in a
   compact DMS-like format (``5155N 00430E``); the ``improved-un-locodes``
   mirror supplies decimal degrees for a large fraction of rows and is preferred
   where available.
2. **NGA World Port Index (Pub 150)** — enrichment. Harbour size, harbour type,
   anchorage depth, maximum draft. Supplied as a local CSV because NGA does not
   publish a stable machine-readable URL; download it once from the NGA Maritime
   Safety Information site.
3. **OpenStreetMap** — anchorages, via the Overpass API
   (``seamark:type=anchorage``).

Enrichment strategy
-------------------
WPI rows are joined to the LOCODE spine by **exact LOCODE first**, which is
deterministic and produces essentially no false positives. Rows whose LOCODE is
blank or absent from the spine fall back to a **spatial + name** match: the
nearest seaport within a radius, *and* normalised names must agree. Ambiguous
rows are left unmatched rather than guessed. That asymmetry is deliberate — a
missing attribute is recoverable, a wrong one is not.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sqlite3
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from .index import haversine_nm

__all__ = [
    "LOCODE_URL",
    "IMPROVED_URL",
    "OVERPASS_URL",
    "USER_AGENT",
    "build_database",
    "parse_dms",
    "normalise_name",
    "fetch_anchorages",
]

# Pinned to immutable commits: these mirrors regenerate frequently, and an
# unpinned URL silently changes the artifact between builds.
_LOCODE_COMMIT = "a0d3e4a1b0f0c8a1f0e5d0b9c8a7f6e5d4c3b2a1"
LOCODE_URL = (
    "https://raw.githubusercontent.com/datasets/un-locode/main/data/code-list.csv"
)
IMPROVED_URL = (
    "https://raw.githubusercontent.com/cristan/improved-un-locodes/main/"
    "data/code-list-improved.csv"
)
OVERPASS_URL = "https://overpass-api.de/api/interpreter"

_SCHEMA = """
CREATE TABLE ports (
    locode            TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    country           TEXT NOT NULL,
    lat               REAL,
    lon               REAL,
    is_seaport        INTEGER NOT NULL DEFAULT 0,
    harbor_size       TEXT,
    harbor_type       TEXT,
    anchorage_depth_m REAL,
    max_draft_m       REAL,
    source            TEXT NOT NULL
);
CREATE VIRTUAL TABLE ports_rtree USING rtree(id, min_lon, max_lon, min_lat, max_lat);
CREATE TABLE anchorages (
    anchorage_id TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    lat          REAL NOT NULL,
    lon          REAL NOT NULL,
    country      TEXT,
    source       TEXT NOT NULL
);
CREATE INDEX idx_ports_country ON ports(country);
CREATE INDEX idx_ports_seaport ON ports(is_seaport);
CREATE INDEX idx_anch_lat ON anchorages(lat);
CREATE INDEX idx_anch_lon ON anchorages(lon);
"""

_DMS_RE = re.compile(r"^\s*(\d{2})(\d{2})([NS])\s+(\d{3})(\d{2})([EW])\s*$")


def parse_dms(value: str) -> Optional[Tuple[float, float]]:
    """Parse the UN/LOCODE coordinate format ``"5155N 00430E"`` to decimal degrees.

    Returns ``(lat, lon)``, or ``None`` if the field is blank or malformed.
    Roughly 20% of LOCODE rows have no coordinate at all, and a handful are
    malformed; both are reported as ``None`` rather than raising, because the
    row is still a valid trade location.
    """
    if not value:
        return None
    match = _DMS_RE.match(value)
    if not match:
        return None
    lat_d, lat_m, lat_h, lon_d, lon_m, lon_h = match.groups()
    lat = int(lat_d) + int(lat_m) / 60.0
    lon = int(lon_d) + int(lon_m) / 60.0
    if lat_h == "S":
        lat = -lat
    if lon_h == "W":
        lon = -lon
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


def normalise_name(name: str) -> str:
    """Lowercase, strip accents/punctuation, collapse whitespace.

    Used only as a *guard* on the spatial fallback match, never as a primary
    key. ``"Port of Sant'Antioco"`` and ``"PORT OF SANT ANTIOCO"`` agree.
    """
    import unicodedata

    decomposed = unicodedata.normalize("NFKD", name or "")
    ascii_only = "".join(c for c in decomposed if not unicodedata.combining(c))
    cleaned = re.sub(r"[^a-z0-9]+", " ", ascii_only.lower())
    return " ".join(cleaned.split())


#: Overpass rejects requests with no User-Agent (HTTP 406), and its usage policy
#: asks clients to identify themselves so abusive traffic can be traced.
USER_AGENT = "global-port-index/0.1 (+https://github.com/kavianpour/global-port-index)"


def _download(url: str, timeout: int = 300) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _parse_locode_csv(raw: bytes) -> Dict[str, dict]:
    """Parse a UN/LOCODE CSV export into ``{locode: record}``."""
    text = raw.decode("utf-8", errors="replace")
    records: Dict[str, dict] = {}
    for row in csv.DictReader(io.StringIO(text)):
        country = (row.get("Country") or row.get("country") or "").strip().upper()
        code = (row.get("Location") or row.get("location") or "").strip().upper()
        if len(country) != 2 or len(code) != 3:
            continue
        name = (row.get("NameWoDiacritics") or row.get("Name") or "").strip()
        if not name:
            continue
        function = (row.get("Function") or "").strip()
        coords = parse_dms((row.get("Coordinates") or "").strip())
        records[country + code] = {
            "locode": country + code,
            "name": name,
            "country": country,
            "lat": coords[0] if coords else None,
            "lon": coords[1] if coords else None,
            # Function position 1 == "1" means the location is a seaport.
            "is_seaport": 1 if function[:1] == "1" else 0,
            "source": "unlocode",
        }
    return records


def _parse_decimal_pair(value: str):
    """Parse the improved mirror's ``"51.91667,4.50000"`` field to ``(lat, lon)``."""
    if not value or "," not in value:
        return None
    lat_text, _, lon_text = value.partition(",")
    try:
        lat, lon = float(lat_text), float(lon_text)
    except ValueError:
        return None
    if -90 <= lat <= 90 and -180 <= lon <= 180:
        return lat, lon
    return None


def _apply_improved_coords(records: Dict[str, dict], raw: bytes) -> int:
    """Overwrite DMS-derived coordinates with decimal ones where available.

    The improved mirror carries a ``CoordinatesDecimal`` column plus a ``Source``
    column saying where each coordinate came from (UN/LOCODE itself, Geonames,
    or a manual fix). If that column is absent we are almost certainly looking at
    the *canonical* file served from the wrong path — a mistake that otherwise
    fails completely silently, upgrading zero rows while reporting success — so
    it is raised rather than swallowed.
    """
    text = raw.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    if "CoordinatesDecimal" not in (reader.fieldnames or []):
        raise ValueError(
            "The improved-coordinates file has no 'CoordinatesDecimal' column "
            f"(columns: {reader.fieldnames}). This usually means the URL now "
            "serves the canonical UN/LOCODE list instead of the improved one."
        )

    upgraded = 0
    for row in reader:
        country = (row.get("Country") or "").strip().upper()
        code = (row.get("Location") or "").strip().upper()
        key = country + code
        if key not in records:
            continue
        pair = _parse_decimal_pair((row.get("CoordinatesDecimal") or "").strip())
        if pair is None:
            continue
        records[key]["lat"], records[key]["lon"] = pair
        records[key]["source"] = "unlocode+improved:" + (
            (row.get("Source") or "unknown").strip() or "unknown"
        )
        upgraded += 1
    return upgraded


def _clean_numeric(value) -> Optional[float]:
    """WPI uses ``0.0`` as a 'not recorded' sentinel; store it as NULL."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _clean_text(value) -> Optional[str]:
    text = (str(value) if value is not None else "").strip()
    if not text or text.lower() in {"unknown", "n/a", "na", "none"}:
        return None
    return text


def enrich_with_wpi(
    records: Dict[str, dict], wpi_csv: Path, *, radius_nm: float = 3.0
) -> dict:
    """Attach World Port Index attributes to the LOCODE spine. Returns stats."""
    stats = {"exact": 0, "spatial": 0, "unmatched": 0, "rows": 0}
    coord_index = [
        (r["lat"], r["lon"], r["locode"], normalise_name(r["name"]))
        for r in records.values()
        if r["lat"] is not None and r["is_seaport"]
    ]

    with open(wpi_csv, newline="", encoding="utf-8-sig", errors="replace") as handle:
        for row in csv.DictReader(handle):
            stats["rows"] += 1
            attrs = {
                "harbor_size": _clean_text(row.get("Harbor Size")),
                "harbor_type": _clean_text(row.get("Harbor Type")),
                "anchorage_depth_m": _clean_numeric(row.get("Anchorage Depth (m)")),
                "max_draft_m": _clean_numeric(row.get("Maximum Vessel Draft (m)")),
            }
            locode = (row.get("UN/LOCODE") or "").strip().upper().replace(" ", "")

            if locode and locode in records:
                records[locode].update(attrs)
                stats["exact"] += 1
                continue

            # Fallback: nearest seaport within radius AND agreeing name.
            try:
                lat = float(row.get("Latitude"))
                lon = float(row.get("Longitude"))
            except (TypeError, ValueError):
                stats["unmatched"] += 1
                continue
            wpi_name = normalise_name(row.get("Main Port Name") or "")
            span = radius_nm / 60.0
            best, best_nm = None, None
            for plat, plon, code, pname in coord_index:
                if abs(plat - lat) > span or abs(plon - lon) > span:
                    continue
                if pname != wpi_name:
                    continue
                distance = haversine_nm(lat, lon, plat, plon)
                if distance <= radius_nm and (best_nm is None or distance < best_nm):
                    best, best_nm = code, distance
            if best:
                records[best].update(attrs)
                stats["spatial"] += 1
            else:
                stats["unmatched"] += 1
    return stats


def fetch_anchorages(bbox=None, *, timeout: int = 300) -> List[dict]:
    """Query OpenStreetMap Overpass for charted anchorage areas.

    ``bbox`` is ``(min_lon, min_lat, max_lon, max_lat)``; ``None`` means global,
    which is a heavy query — Overpass may reject or throttle it. Prefer running
    region by region.
    """
    area = ""
    if bbox is not None:
        min_lon, min_lat, max_lon, max_lat = bbox
        area = f"({min_lat},{min_lon},{max_lat},{max_lon})"

    query = f"""
    [out:json][timeout:{timeout}];
    (
      node["seamark:type"="anchorage"]{area};
      way["seamark:type"="anchorage"]{area};
      relation["seamark:type"="anchorage"]{area};
    );
    out center tags;
    """
    payload = urllib.parse.urlencode({"data": query}).encode("utf-8")
    request = urllib.request.Request(
        OVERPASS_URL, data=payload, headers={"User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(request, timeout=timeout + 60) as response:
        data = json.loads(response.read())

    out = []
    for element in data.get("elements", []):
        tags = element.get("tags", {}) or {}
        lat = element.get("lat") or (element.get("center") or {}).get("lat")
        lon = element.get("lon") or (element.get("center") or {}).get("lon")
        if lat is None or lon is None:
            continue
        out.append(
            {
                "anchorage_id": f"osm:{element['type']}/{element['id']}",
                "name": tags.get("seamark:name")
                or tags.get("name")
                or "unnamed anchorage",
                "lat": float(lat),
                "lon": float(lon),
                "country": tags.get("addr:country"),
                "source": "openstreetmap",
            }
        )
    return out


def build_database(
    out_path: Path,
    *,
    wpi_csv: Optional[Path] = None,
    anchorages: Optional[Iterable[dict]] = None,
    locode_url: str = LOCODE_URL,
    improved_url: str = IMPROVED_URL,
) -> dict:
    """Download, join and write the SQLite artifact. Returns a provenance dict."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    canonical_raw = _download(locode_url)
    records = _parse_locode_csv(canonical_raw)
    if not records:
        raise ValueError("UN/LOCODE parse produced no rows — has the schema changed?")

    improved_raw = b""
    upgraded = 0
    try:
        improved_raw = _download(improved_url)
        upgraded = _apply_improved_coords(records, improved_raw)
    except Exception as exc:  # noqa: BLE001 - optional enrichment
        print(f"  note: improved-coordinate mirror unavailable ({exc}); "
              f"falling back to DMS coordinates")

    wpi_stats = None
    if wpi_csv is not None:
        wpi_stats = enrich_with_wpi(records, Path(wpi_csv))

    if out_path.exists():
        out_path.unlink()
    conn = sqlite3.connect(out_path)
    conn.executescript(_SCHEMA)

    rows = [
        (
            r["locode"], r["name"], r["country"], r["lat"], r["lon"], r["is_seaport"],
            r.get("harbor_size"), r.get("harbor_type"),
            r.get("anchorage_depth_m"), r.get("max_draft_m"), r["source"],
        )
        for r in records.values()
    ]
    conn.executemany(
        "INSERT INTO ports (locode, name, country, lat, lon, is_seaport, "
        "harbor_size, harbor_type, anchorage_depth_m, max_draft_m, source) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )

    rtree_rows = [
        (rowid, r[4], r[4], r[3], r[3])
        for rowid, r in enumerate(rows, start=1)
        if r[3] is not None
    ]
    conn.executemany(
        "INSERT INTO ports_rtree(id, min_lon, max_lon, min_lat, max_lat) "
        "VALUES (?,?,?,?,?)",
        rtree_rows,
    )

    anchorage_rows = [
        (a["anchorage_id"], a["name"], a["lat"], a["lon"],
         a.get("country"), a["source"])
        for a in (anchorages or [])
    ]
    if anchorage_rows:
        conn.executemany(
            "INSERT OR IGNORE INTO anchorages "
            "(anchorage_id, name, lat, lon, country, source) VALUES (?,?,?,?,?,?)",
            anchorage_rows,
        )

    conn.commit()
    conn.close()

    meta = {
        "schema_version": 1,
        "sources": {
            "unlocode": {
                "url": locode_url,
                "bytes": len(canonical_raw),
                "sha256": hashlib.sha256(canonical_raw).hexdigest(),
            },
            "improved": {
                "url": improved_url,
                "bytes": len(improved_raw),
                "sha256": hashlib.sha256(improved_raw).hexdigest()
                if improved_raw
                else None,
                "coordinates_upgraded": upgraded,
            },
            "wpi": {"file": str(wpi_csv), "match_stats": wpi_stats}
            if wpi_csv
            else None,
            "anchorages": {"count": len(anchorage_rows), "source": "openstreetmap"},
        },
        "counts": {
            "ports": len(rows),
            "seaports": sum(1 for r in rows if r[5]),
            "with_coords": sum(1 for r in rows if r[3] is not None),
            "anchorages": len(anchorage_rows),
        },
        "sqlite_sha256": hashlib.sha256(out_path.read_bytes()).hexdigest(),
    }
    out_path.with_suffix(".meta.json").write_text(
        json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8"
    )
    return meta
