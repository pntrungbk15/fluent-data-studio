"""Synthetic demo datasets, generated deterministically (no real company or personal data).

``python -m fluent_data_studio generate-demo`` rewrites the CSV files and their semantic sidecars in
``fluent_data_studio/datasets``. Each dataset has patterns worth finding: growth that differs by product and region,
seasonality, a carrier that is late more often, spend with diminishing returns, a production line whose defects follow
temperature, a few outliers and some missing values.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import math
import random
import sqlite3
from pathlib import Path
from typing import Dict, List

__all__ = ["DATASETS_DIR", "generate_all", "demo_files", "build_demo_sqlite"]

DATASETS_DIR = Path(__file__).resolve().parent / "datasets"

_REGIONS = {"North": 1.0, "South": 0.85, "East": 1.1, "West": 0.9, "Central": 0.7}
_REGION_GROWTH = {"North": 0.04, "South": 0.02, "East": 0.08, "West": 0.27, "Central": -0.06}
_CHANNELS = {"Online": 0.5, "Store": 0.35, "Partner": 0.15}
_PRODUCTS = [
    # product, category, list price, unit cost ratio, 2025 growth, launch
    ("Aurora Laptop 14", "Electronics", 1099.0, 0.78, 0.12, "2023-03-01"),
    ("Aurora Laptop 16", "Electronics", 1499.0, 0.80, 0.05, "2023-03-01"),
    ("Pulse Earbuds", "Electronics", 129.0, 0.55, 0.62, "2024-02-15"),
    ("Pulse Headphones", "Electronics", 249.0, 0.58, 0.08, "2022-09-01"),
    ("Vista Monitor 27", "Electronics", 329.0, 0.70, -0.10, "2022-01-10"),
    ("Nimbus Tablet", "Electronics", 499.0, 0.72, 0.31, "2023-10-01"),
    ("Orbit Smartwatch", "Electronics", 279.0, 0.60, 0.48, "2024-04-01"),
    ("Cedar Desk", "Office", 389.0, 0.52, 0.03, "2021-05-01"),
    ("Cedar Standing Desk", "Office", 649.0, 0.55, 0.41, "2023-06-01"),
    ("Ergo Chair", "Office", 459.0, 0.50, 0.15, "2021-05-01"),
    ("Paper Pack A4", "Office", 9.5, 0.40, -0.04, "2020-01-01"),
    ("Ink Cartridge Set", "Office", 39.0, 0.35, -0.22, "2020-01-01"),
    ("Desk Lamp Halo", "Office", 59.0, 0.45, 0.06, "2022-03-01"),
    ("Linen Bedding Set", "Home", 119.0, 0.42, 0.09, "2022-02-01"),
    ("Cast Iron Pan", "Home", 49.0, 0.38, 0.02, "2021-01-01"),
    ("Air Purifier Breeze", "Home", 219.0, 0.57, 0.55, "2024-01-20"),
    ("Coffee Maker Brew", "Home", 89.0, 0.48, -0.03, "2021-08-01"),
    ("Robot Vacuum Dash", "Home", 349.0, 0.62, 0.36, "2023-09-01"),
    ("Trail Backpack 30L", "Outdoor", 99.0, 0.44, 0.11, "2022-04-01"),
    ("Summit Tent 2P", "Outdoor", 249.0, 0.50, 0.04, "2021-04-01"),
    ("Trek Poles", "Outdoor", 69.0, 0.40, -0.08, "2021-04-01"),
    ("Hydro Bottle 1L", "Outdoor", 25.0, 0.30, 0.22, "2022-06-01"),
    ("E-Bike Volt", "Outdoor", 1899.0, 0.74, 0.58, "2024-03-01"),
    ("Rain Jacket Storm", "Apparel", 139.0, 0.41, 0.07, "2021-09-01"),
    ("Merino Base Layer", "Apparel", 79.0, 0.39, 0.13, "2022-10-01"),
    ("Running Shoes Glide", "Apparel", 129.0, 0.47, 0.19, "2022-02-01"),
    ("Everyday Tee", "Apparel", 19.0, 0.30, -0.12, "2020-01-01"),
    ("Wool Socks 3-Pack", "Apparel", 24.0, 0.33, 0.01, "2020-01-01"),
]
_SUPPLIERS = {"Electronics": ["Kestrel Components", "Lumen Devices"], "Office": ["Oakline Supply"],
              "Home": ["Hearth & Co", "Kestrel Components"], "Outdoor": ["Ridge Gear"], "Apparel": ["Loom Works"]}
_SEASON = [0.82, 0.78, 0.92, 0.95, 1.0, 0.97, 0.94, 1.02, 1.0, 1.05, 1.32, 1.55]


def _write_csv(path: Path, header: List[str], rows: List[list]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def _write_semantics(path: Path, description: str, columns: Dict[str, dict]) -> None:
    sidecar = path.with_name(path.stem + ".semantic.json")
    sidecar.write_text(json.dumps({"description": description, "columns": columns}, indent=2) + "\n", encoding="utf-8")


def _weighted(rng: random.Random, weights: Dict[str, float]) -> str:
    return rng.choices(list(weights), weights=list(weights.values()))[0]


def generate_retail(folder: Path) -> None:
    rng = random.Random(2026)
    rows: List[list] = []
    order_id = 100000
    start = dt.date(2024, 1, 1)
    weights = [1.0 / (1 + i % 7) + (0.6 if p[0] in ("Pulse Earbuds", "Hydro Bottle 1L", "Paper Pack A4") else 0)
               for i, p in enumerate(_PRODUCTS)]
    for day in range((dt.date(2025, 12, 31) - start).days + 1):
        date = start + dt.timedelta(days=day)
        base = 26 * _SEASON[date.month - 1] * (1.15 if date.weekday() >= 5 else 1.0)
        for _ in range(int(rng.gauss(base, base * 0.12))):
            order_id += 1
            region = _weighted(rng, _REGIONS)
            product = rng.choices(_PRODUCTS, weights=weights)[0]
            name, category, price, cost_ratio, growth, launch = product
            if date < dt.date.fromisoformat(launch):
                continue
            # growth acts as extra probability of keeping 2025 orders (and region growth on top)
            if date.year == 2025:
                keep = (1 + growth) * (1 + _REGION_GROWTH[region])
                if rng.random() > keep / 2.2:
                    continue
            elif rng.random() > 1 / 2.2:
                continue
            channel = _weighted(rng, _CHANNELS)
            units = max(1, int(rng.expovariate(0.9)) + 1)
            if price < 150 and rng.random() < 0.004:  # occasional bulk orders of cheap items: genuine outliers
                units = rng.randint(40, 120)
            discount = rng.choice([0, 0, 0, 0.05, 0.1, 0.15, 0.2]) if channel != "Partner" else rng.choice([0.1, 0.15, 0.25])
            if date.month == 11 and date.day >= 24:
                discount = max(discount, 0.2)
            unit_price = round(price * rng.uniform(0.97, 1.03), 2)
            revenue = round(units * unit_price * (1 - discount), 2)
            cost = round(units * price * cost_ratio, 2)
            status = rng.choices(["Completed", "Shipped", "Cancelled", "Returned"], weights=[86, 5, 6, 3])[0]
            if date >= dt.date(2025, 12, 20) and status == "Completed":
                status = "Shipped"
            customer = f"C{rng.randint(1, 4200):05d}"
            rows.append([order_id, date.isoformat(), customer, region, channel, category, name, units, unit_price,
                         "" if rng.random() < 0.015 else discount, revenue, cost, status])
    path = folder / "retail_orders.csv"
    _write_csv(path, ["order_id", "order_date", "customer_id", "region", "channel", "category", "product", "units",
                      "unit_price", "discount", "revenue", "cost", "status"], rows)
    _write_semantics(path, "Customer orders of a fictional multi-channel retailer, 2024 to 2025. One row per order line.", {
        "order_date": {"description": "Date the order was placed", "synonyms": ["date", "order day"]},
        "revenue": {"description": "Net revenue after discount", "format": "currency", "synonyms": ["sales", "turnover", "income"]},
        "cost": {"description": "Cost of goods sold", "format": "currency", "synonyms": ["cogs"]},
        "units": {"description": "Units sold", "aggregation": "sum", "synonyms": ["quantity", "qty", "volume"]},
        "unit_price": {"description": "Price per unit before discount", "aggregation": "avg", "format": "currency", "synonyms": ["price"]},
        "discount": {"description": "Discount rate applied (0.1 = 10%); missing when not recorded", "aggregation": "avg", "format": "percent"},
        "status": {"description": "Order status: Completed, Shipped, Cancelled or Returned"},
        "customer_id": {"role": "identifier", "description": "Pseudonymous customer key", "synonyms": ["customer"]},
        "product": {"synonyms": ["item", "sku name"]},
        "category": {"synonyms": ["product category", "segment"]},
        "region": {"synonyms": ["area", "territory"]},
        "channel": {"synonyms": ["sales channel"]},
    })
    products = [[p[0], p[1], p[2], round(p[2] * p[3], 2), _SUPPLIERS[p[1]][i % len(_SUPPLIERS[p[1]])], p[5]]
                for i, p in enumerate(_PRODUCTS)]
    path = folder / "products.csv"
    _write_csv(path, ["product", "category", "list_price", "unit_cost", "supplier", "launch_date"], products)
    _write_semantics(path, "Product catalogue of the fictional retailer; joins to retail_orders on product.", {
        "list_price": {"aggregation": "avg", "format": "currency"},
        "unit_cost": {"aggregation": "avg", "format": "currency"},
        "product": {"role": "dimension"},
    })


_PORTS = ["Ho Chi Minh City", "Singapore", "Shanghai", "Busan", "Rotterdam", "Los Angeles", "Hamburg", "Dubai"]
_CARRIERS = {"BlueWave Lines": 0.93, "Meridian Freight": 0.88, "Northstar Logistics": 0.74, "Swift Cargo": 0.9,
             "Atlas Air Cargo": 0.95}


def generate_shipments(folder: Path) -> None:
    rng = random.Random(77)
    rows: List[list] = []
    start = dt.date(2024, 1, 1)
    for i in range(9000):
        date = start + dt.timedelta(days=rng.randint(0, 729))
        origin, destination = rng.sample(_PORTS, 2)
        carrier = rng.choice(list(_CARRIERS))
        mode = "Air" if carrier == "Atlas Air Cargo" else rng.choices(["Sea", "Road", "Rail"], weights=[70, 18, 12])[0]
        distance = rng.randint(800, 18000) if mode in ("Sea", "Air") else rng.randint(150, 2500)
        weight = round(rng.lognormvariate(7.2 if mode != "Air" else 5.5, 0.8), 1)
        speed = {"Sea": 650, "Air": 9000, "Road": 600, "Rail": 800}[mode]
        promised = max(1, round(distance / speed + (2 if mode == "Sea" else 1)))
        on_time_p = _CARRIERS[carrier] - (0.12 if date.month in (1, 2) and mode == "Sea" else 0)
        on_time = rng.random() < on_time_p
        delay = 0 if on_time else rng.choice([1, 1, 2, 2, 3, 4, 6, 9])
        transit = max(1, promised - (rng.random() < 0.3) + delay)
        reason = "" if on_time else rng.choices(["Port congestion", "Weather", "Customs", "Equipment", "Documentation"],
                                                weights=[35, 20, 22, 13, 10])[0]
        rate = {"Sea": 0.00009, "Air": 0.0011, "Road": 0.00035, "Rail": 0.00022}[mode]
        cost = round(weight * distance * rate * rng.uniform(0.85, 1.2) + 120, 2)
        rows.append([f"SH{24000 + i}", date.isoformat(), origin, destination, carrier, mode, weight, distance,
                     transit, promised, cost, "true" if transit <= promised else "false", reason])
    rows.sort(key=lambda r: r[1])
    path = folder / "shipments.csv"
    _write_csv(path, ["shipment_id", "ship_date", "origin", "destination", "carrier", "mode", "weight_kg",
                      "distance_km", "transit_days", "promised_days", "cost_usd", "on_time", "delay_reason"], rows)
    _write_semantics(path, "Freight shipments of a fictional forwarder, 2024 to 2025.", {
        "transit_days": {"aggregation": "avg", "description": "Days from pickup to delivery"},
        "promised_days": {"aggregation": "avg", "description": "Days promised at booking"},
        "cost_usd": {"format": "currency", "synonyms": ["freight cost", "cost", "spend"]},
        "weight_kg": {"synonyms": ["weight"]},
        "distance_km": {"aggregation": "avg", "synonyms": ["distance"]},
        "on_time": {"description": "Delivered within the promised days", "synonyms": ["on-time", "punctuality"]},
        "delay_reason": {"description": "Main cause of a late delivery; empty when on time"},
    })


_CAMPAIGNS = [("Spring Launch", "Search"), ("Always-on Brand", "Display"), ("Retargeting", "Social"),
              ("Newsletter", "Email"), ("Creator Series", "Video"), ("Holiday Push", "Social"),
              ("Competitor Terms", "Search")]


def generate_marketing(folder: Path) -> None:
    rng = random.Random(11)
    rows: List[list] = []
    start = dt.date(2024, 1, 1)
    efficiency = {"Search": 0.05, "Display": 0.012, "Social": 0.03, "Email": 0.09, "Video": 0.018}
    for day in range(731):
        date = start + dt.timedelta(days=day)
        for name, channel in _CAMPAIGNS:
            if name == "Holiday Push" and date.month not in (11, 12):
                continue
            if name == "Spring Launch" and date.month not in (3, 4, 5):
                continue
            spend = max(0.0, rng.gauss({"Email": 60, "Search": 900, "Display": 500, "Social": 700, "Video": 650}[channel],
                                       120)) * _SEASON[date.month - 1]
            impressions = int(spend * rng.uniform(80, 140) * (3 if channel in ("Display", "Video") else 1))
            clicks = int(impressions * efficiency[channel] * rng.uniform(0.7, 1.3))
            # diminishing returns: conversions grow with the square root of spend
            conversions = int(math.sqrt(spend) * {"Search": 1.4, "Display": 0.35, "Social": 0.8, "Email": 2.2,
                                                   "Video": 0.45}[channel] * rng.uniform(0.6, 1.4))
            revenue = round(conversions * rng.uniform(60, 140), 2)
            rows.append([date.isoformat(), name, channel, round(spend, 2), impressions, clicks, conversions, revenue])
    path = folder / "marketing_campaigns.csv"
    _write_csv(path, ["date", "campaign", "channel", "spend", "impressions", "clicks", "conversions", "revenue"], rows)
    _write_semantics(path, "Daily performance of a fictional company's marketing campaigns, 2024 to 2025.", {
        "spend": {"format": "currency", "synonyms": ["cost", "budget", "ad spend"]},
        "revenue": {"format": "currency", "description": "Attributed revenue", "synonyms": ["sales"]},
        "conversions": {"synonyms": ["orders", "signups"]},
    })


def generate_production(folder: Path) -> None:
    rng = random.Random(5)
    rows: List[list] = []
    start = dt.datetime(2025, 7, 1)
    for hour in range(24 * 92):
        moment = start + dt.timedelta(hours=hour)
        shift = "Night" if moment.hour < 6 or moment.hour >= 22 else ("Day" if moment.hour < 14 else "Evening")
        for line in ("L1", "L2", "L3", "L4"):
            temperature = rng.gauss(24 if line != "L3" else 27.5, 1.6) + (2.5 if shift == "Day" else 0)
            vibration = abs(rng.gauss(2.2 if line != "L2" else 3.1, 0.5))
            downtime = 0 if rng.random() > 0.08 else rng.choice([5, 10, 15, 30, 45])
            if line == "L2" and dt.date(2025, 8, 12) <= moment.date() <= dt.date(2025, 8, 14):
                downtime = rng.choice([20, 35, 50])  # a maintenance problem worth finding
                vibration += 2.5
            units = int(max(0, rng.gauss(120, 8) * (60 - downtime) / 60))
            defect_rate = 0.008 + max(0.0, temperature - 25) * 0.006 + vibration * 0.002
            defects = sum(1 for _ in range(units) if rng.random() < defect_rate)
            product = "Housing A" if line in ("L1", "L2") else "Bracket B"
            rows.append([moment.strftime("%Y-%m-%d %H:%M:%S"), line, shift, product, units, defects, downtime,
                         round(temperature, 2), round(vibration, 3)])
    path = folder / "production_line.csv"
    _write_csv(path, ["timestamp", "line", "shift", "product_type", "units_produced", "defects", "downtime_minutes",
                      "temperature_c", "vibration_mm_s"], rows)
    _write_semantics(path, "Hourly output of four fictional production lines, July to September 2025.", {
        "units_produced": {"synonyms": ["output", "production", "units"]},
        "defects": {"synonyms": ["rejects", "scrap"]},
        "downtime_minutes": {"synonyms": ["downtime", "stoppage"]},
        "temperature_c": {"aggregation": "avg", "synonyms": ["temperature"]},
        "vibration_mm_s": {"aggregation": "avg", "synonyms": ["vibration"]},
        "line": {"synonyms": ["production line"]},
    })


def generate_all(folder: Path = DATASETS_DIR) -> List[Path]:
    generate_retail(folder)
    generate_shipments(folder)
    generate_marketing(folder)
    generate_production(folder)
    return demo_files(folder)


def demo_files(folder: Path = DATASETS_DIR) -> List[Path]:
    return sorted(folder.glob("*.csv"))


def build_demo_sqlite(target: Path, folder: Path = DATASETS_DIR) -> Path:
    """A SQLite copy of the retail data (orders and products) for trying the database connector."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    con = sqlite3.connect(target)
    try:
        for name, types in (("retail_orders", {"order_id": "INTEGER", "units": "INTEGER", "unit_price": "REAL",
                                                "discount": "REAL", "revenue": "REAL", "cost": "REAL"}),
                            ("products", {"list_price": "REAL", "unit_cost": "REAL"})):
            with (folder / f"{name}.csv").open(encoding="utf-8") as handle:
                reader = csv.reader(handle)
                header = next(reader)
                columns = ", ".join(f'"{c}" {types.get(c, "TEXT")}' for c in header)
                con.execute(f'CREATE TABLE "{name}" ({columns})')
                con.executemany(f'INSERT INTO "{name}" VALUES ({", ".join("?" for _ in header)})',
                                ([v if v != "" else None for v in row] for row in reader))
        con.execute('CREATE VIEW "revenue_by_region" AS SELECT region, sum(revenue) AS revenue FROM retail_orders '
                    "WHERE status != 'Cancelled' GROUP BY region")
        con.commit()
    finally:
        con.close()
    return target
