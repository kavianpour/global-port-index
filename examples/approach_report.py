"""
Report the nearest port and anchorage to a position, with harbour attributes.

Run:  python examples/approach_report.py
Requires a built database:
    python -m port_index build --wpi UpdatedPub150.csv --out data/ports.sqlite
"""
import sys

from port_index import PortIndex, haversine_nm

DB = sys.argv[1] if len(sys.argv) > 1 else "data/ports.sqlite"
POSITIONS = [
    ("Maas approach", 51.98, 3.60),
    ("Singapore Strait", 1.24, 103.85),
    ("Gibraltar", 36.05, -5.40),
]

with PortIndex(DB) as idx:
    print(f"database: {idx.stats()}\n")

    for label, lat, lon in POSITIONS:
        print(f"{label}  ({lat}, {lon})")

        port = idx.nearest(lat, lon)
        if port is None:
            print("  no seaport within range\n")
            continue
        distance = haversine_nm(lat, lon, port.lat, port.lon)
        print(f"  nearest port : {port.name} [{port.locode}]  {distance:.1f} nm")
        if port.harbor_size:
            print(f"    size       : {port.harbor_size} / {port.harbor_type}")
        if port.anchorage_depth_m:
            print(f"    anch. depth: {port.anchorage_depth_m} m")
        print(f"    provenance : {port.source}")

        anchorage = idx.nearest_anchorage(lat, lon, max_nm=60)
        if anchorage:
            d = haversine_nm(lat, lon, anchorage.lat, anchorage.lon)
            print(f"  nearest anchorage: {anchorage.name}  {d:.1f} nm")
        print()
