"""
port-index — a queryable global index of seaports and anchorages.

Built from UN/LOCODE (spine), the NGA World Port Index (attributes) and
OpenStreetMap (anchorages). Stdlib-only at runtime.

    python -m port_index build --wpi UpdatedPub150.csv --out data/ports.sqlite

    from port_index import PortIndex
    idx = PortIndex("data/ports.sqlite")
    idx.resolve("NLRTM")
    idx.nearest(51.95, 4.10)
"""

from .build import build_database, fetch_anchorages, normalise_name, parse_dms
from .index import (
    AnchorageRecord,
    PortIndex,
    PortRecord,
    default_db_path,
    haversine_nm,
)

__version__ = "0.1.0"

__all__ = [
    "AnchorageRecord",
    "PortIndex",
    "PortRecord",
    "build_database",
    "default_db_path",
    "fetch_anchorages",
    "haversine_nm",
    "normalise_name",
    "parse_dms",
    "__version__",
]
