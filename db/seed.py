"""
Seed the database.

Locations are REAL, pulled live from OpenStreetMap (Overpass API):
  warehouses   building=warehouse / industrial=warehouse   -> our distribution hubs
  suppliers    named shop=wholesale / shop=trade           -> backup suppliers
  stores       named shop=supermarket                      -> delivery destinations

Inventory levels and shipments are the distributor's operational records
(in production these come from the company's ERP). They are generated once
here, stored in the live database, and editable from the app.

Run (from the project root):
  python -m db.seed            add data
  python -m db.seed --reset    wipe tables first
  python -m db.seed --refresh  re-download OSM data instead of using the cache
"""
from __future__ import annotations

import json
import random
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from db.queries import _conn          # noqa: E402
from tools.http import request        # noqa: E402

CACHE = Path(__file__).with_name("osm_cache.json")
OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
CITIES = {
    "Ahmedabad": (23.0225, 72.5714),
    "Vadodara": (22.3072, 73.1812),
    "Surat": (21.1702, 72.8311),
    "Rajkot": (22.3039, 70.8022),
    "Anand": (22.5645, 72.9289),
    "Bharuch": (21.7051, 72.9959),
    "Bhavnagar": (21.7645, 72.1519),
    "Mumbai": (19.0760, 72.8777),
}
HUB_CITIES = ["Ahmedabad", "Surat", "Rajkot", "Mumbai"]
# Ahmedabad -> these cities runs down NH-48 past Nadiad/Anand: the demo corridor
CORRIDOR = ["Vadodara", "Bharuch", "Surat", "Mumbai"]
SKUS = ["RICE-25KG", "WHEAT-ATTA-10KG", "SUNFLOWER-OIL-15L", "SUGAR-50KG",
        "TOOR-DAL-30KG", "SALT-1KG-CASE", "TEA-500G-CASE", "DETERGENT-5KG"]
RADIUS_M = 15000
SUPPLIER_RADIUS_M = 25000
# OSM tags are sometimes wrong (a spa tagged as a supermarket). Names with these
# words are not food/FMCG businesses, so they are excluded.
NOT_FMCG = ("electric", "plywood", "ply ", "building", "lime", "hardware", "cement",
            "paint", "steel", "tile", "sanitary", "spa", "salon", "beauty", "mobile",
            "auto", "motor", "glass", "marble", "timber", "furniture", "tyre", "sport",
            "decath", "dechat", "fashion", "garment", "jewel", "optical", "book")
CONVENIENCE_OK = ("trader", "general", "kirana", "provision", "agenc", "wholesale",
                  "enterprise", "store", "mart", "supply", "suppliers", "distribut")


GENERIC = {"store", "shop", "mart", "general store", "kirana", "kirana store",
           "supermarket", "super market", "provision store", "traders", "wholesale"}


def _is_fmcg(name: str) -> bool:
    n = name.lower().strip(" .,-")
    if n in GENERIC or len(n) < 5:      # placeholder names like "store" look broken
        return False
    return not any(w in n for w in NOT_FMCG)
rng = random.Random(26)   # fixed seed: the same data every run


# ---------- OpenStreetMap ----------

def _query(lat: float, lon: float) -> str:
    a = f"(around:{RADIUS_M},{lat},{lon})"
    b = f"(around:{SUPPLIER_RADIUS_M},{lat},{lon})"
    return f"""
    [out:json][timeout:90];
    (nwr["building"="warehouse"]{a}; nwr["industrial"="warehouse"]{a};)->.wh;
    (nwr["shop"~"^(wholesale|trade|convenience)$"]["name"]{b};
     nwr["wholesale"]["name"]{b};)->.sup;
    (nwr["shop"="supermarket"]["name"]{a};)->.store;
    .wh out center 10;
    .sup out center 40;
    .store out center 8;
    """


def _overpass(q: str) -> list[dict]:
    errors = []
    for url in OVERPASS_MIRRORS:
        try:
            return request("POST", url, data={"data": q}, timeout=90, retries=1).json()["elements"]
        except Exception as e:
            errors.append(f"{url.split('/')[2]}: {str(e)[:80]}")
    raise RuntimeError("All Overpass mirrors failed: " + " | ".join(errors))


def _classify(el: dict, city: str) -> tuple[str, dict] | None:
    tags = el.get("tags", {})
    lat = el.get("lat") or (el.get("center") or {}).get("lat")
    lon = el.get("lon") or (el.get("center") or {}).get("lon")
    if lat is None or lon is None:
        return None
    osm_id = f"{el['type']}/{el['id']}"
    name = tags.get("name")
    if tags.get("shop") == "supermarket":
        kind = "store"
    elif tags.get("shop") in ("wholesale", "trade") or "wholesale" in tags:
        kind = "supplier"
    elif tags.get("shop") == "convenience":
        # small shops count only if the name says trader/distributor/general store
        if not name or not any(w in name.lower() for w in CONVENIENCE_OK):
            return None
        kind = "supplier"
    elif tags.get("building") == "warehouse" or tags.get("industrial") == "warehouse":
        kind = "warehouse"
        name = name or f"{city} Warehouse (OSM {el['id']})"
    else:
        return None
    if not name:
        return None
    return kind, {"name": name, "lat": lat, "lon": lon, "osm_id": osm_id, "city": city}


def fetch_osm(refresh: bool = False) -> dict[str, list[dict]]:
    if CACHE.exists() and not refresh:
        print(f"Using cached OSM data from {CACHE.name} (use --refresh to re-download)")
        return json.loads(CACHE.read_text(encoding="utf-8"))

    found = {"warehouse": [], "supplier": [], "store": []}
    seen = set()
    for city, (lat, lon) in CITIES.items():
        print(f"  OpenStreetMap: {city} ...", end=" ", flush=True)
        try:
            elements = _overpass(_query(lat, lon))
        except RuntimeError as e:
            print(f"skipped ({e})")
            continue
        counts = {"warehouse": 0, "supplier": 0, "store": 0}
        for el in elements:
            item = _classify(el, city)
            if item and item[1]["osm_id"] not in seen:
                seen.add(item[1]["osm_id"])
                found[item[0]].append(item[1])
                counts[item[0]] += 1
        print(counts)
        time.sleep(1.5)   # be polite to the public Overpass servers

    CACHE.write_text(json.dumps(found, indent=1, ensure_ascii=False), encoding="utf-8")
    return found


# ---------- operational records ----------

def build_records(osm: dict) -> dict:
    by_city = lambda kind: {c: [x for x in osm[kind] if x["city"] == c and _is_fmcg(x["name"])]
                            for c in CITIES}
    wh, st = by_city("warehouse"), by_city("store")

    hubs = [wh[c][0] for c in HUB_CITIES if wh[c]]
    if not hubs or not any(h["city"] == "Ahmedabad" for h in hubs):
        raise RuntimeError("No Ahmedabad warehouse found in OSM data; try --refresh")

    suppliers = []
    for c in CITIES:
        suppliers += [x for x in osm["supplier"] if x["city"] == c and _is_fmcg(x["name"])][:6]
    if len(suppliers) < 5:
        raise RuntimeError(f"Only {len(suppliers)} suppliers found; try --refresh")

    # Every SKU stocked by several suppliers, so re-sourcing has real choices
    inventory = []
    for i, s in enumerate(suppliers):
        carried = set(rng.sample(SKUS, rng.randint(3, 5)))
        carried.add(SKUS[i % len(SKUS)])
        for sku in carried:
            inventory.append({"osm_id": s["osm_id"], "sku": sku, "qty": rng.randrange(150, 1200, 10)})

    stores = [x for c in CITIES for x in st[c][:4]]
    ahmedabad = next(h for h in hubs if h["city"] == "Ahmedabad")
    shipments = []

    # Demo corridor: Ahmedabad hub -> one store in each corridor city
    for city in CORRIDOR:
        if st[city]:
            shipments.append((ahmedabad, st[city][0]))
    # Other traffic between hubs and stores in different cities
    while len(shipments) < 15 and stores:
        hub, store = rng.choice(hubs), rng.choice(stores)
        if hub["city"] != store["city"]:
            shipments.append((hub, store))

    shipment_rows = [{"origin_osm": h["osm_id"], "sku": rng.choice(SKUS),
                      "qty": rng.randrange(20, 200, 5), "dest": s} for h, s in shipments]
    return {"hubs": hubs, "suppliers": suppliers, "inventory": inventory, "shipments": shipment_rows}


# ---------- write ----------

def write(records: dict, reset: bool):
    with _conn() as c:
        if reset:
            c.execute("truncate purchase_orders, shipments, inventory, suppliers, "
                      "warehouses, agent_runs restart identity cascade")
            print("Tables cleared")

        wh_ids = {}
        for h in records["hubs"]:
            row = c.execute("""
                insert into warehouses (name, lat, lon, osm_id) values (%s, %s, %s, %s)
                on conflict (osm_id) do update set name = excluded.name
                returning id""", (h["name"], h["lat"], h["lon"], h["osm_id"])).fetchone()
            wh_ids[h["osm_id"]] = row["id"]

        sup_ids = {}
        for s in records["suppliers"]:
            row = c.execute("""
                insert into suppliers (name, lat, lon, osm_id) values (%s, %s, %s, %s)
                on conflict (osm_id) do update set name = excluded.name
                returning id""", (s["name"], s["lat"], s["lon"], s["osm_id"])).fetchone()
            sup_ids[s["osm_id"]] = row["id"]

        for i in records["inventory"]:
            c.execute("""
                insert into inventory (supplier_id, sku, qty) values (%s, %s, %s)
                on conflict (supplier_id, sku) do update set qty = excluded.qty, updated_at = now()
            """, (sup_ids[i["osm_id"]], i["sku"], i["qty"]))

        for s in records["shipments"]:
            d = s["dest"]
            c.execute("""
                insert into shipments (sku, qty, origin_id, dest_name, dest_lat, dest_lon)
                values (%s, %s, %s, %s, %s, %s)""",
                      (s["sku"], s["qty"], wh_ids[s["origin_osm"]],
                       f"{d['name']}, {d['city']}", d["lat"], d["lon"]))

    print(f"Seeded {len(records['hubs'])} warehouses, {len(records['suppliers'])} suppliers, "
          f"{len(records['inventory'])} inventory rows, {len(records['shipments'])} shipments")


if __name__ == "__main__":
    osm = fetch_osm(refresh="--refresh" in sys.argv)
    print(f"OSM: {len(osm['warehouse'])} warehouses, {len(osm['supplier'])} suppliers, "
          f"{len(osm['store'])} stores")
    records = build_records(osm)
    write(records, reset="--reset" in sys.argv)
