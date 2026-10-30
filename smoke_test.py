"""
Tonight's smoke test: every external dependency must PASS before we build anything.
Run:  python smoke_test.py
"""
import os
import sys
import requests
from dotenv import load_dotenv

load_dotenv()

HEADERS = {"User-Agent": "forge26-supply-agent/0.1"}
TIMEOUT = 25


def _ok(r):
    """Like raise_for_status, but shows the server's actual error message."""
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
    return r

# (lat, lon) - Ahmedabad -> Vadodara is our test corridor
AHMEDABAD = (23.0225, 72.5714)
VADODARA = (22.3072, 73.1812)


def open_meteo():
    r = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": AHMEDABAD[0],
            "longitude": AHMEDABAD[1],
            "hourly": "precipitation,wind_speed_10m",
            "forecast_days": 2,
            "timezone": "Asia/Kolkata",
        },
        headers=HEADERS,
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    h = r.json()["hourly"]
    return f"{len(h['time'])} hourly points, max rain {max(h['precipitation'])} mm"


def gdacs():
    try:
        r = requests.get(
            "https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH",
            params={"eventlist": "EQ;TC;FL;VO;DR;WF", "alertlevel": "Green;Orange;Red"},
            headers=HEADERS,
            timeout=TIMEOUT,
        )
        _ok(r)
        feats = r.json().get("features", [])
        types = sorted({f["properties"].get("eventtype", "?") for f in feats})
        return f"API: {len(feats)} events, types: {types}"
    except Exception as api_err:
        # Fallback: GDACS public RSS feed
        import xml.etree.ElementTree as ET

        r = requests.get("https://www.gdacs.org/xml/rss.xml", headers=HEADERS, timeout=TIMEOUT)
        _ok(r)
        items = ET.fromstring(r.content).findall(".//item")
        return f"RSS fallback: {len(items)} events (API error: {str(api_err)[:120]})"


def _ors_route(avoid_polygon=None):
    body = {
        # ORS uses [lon, lat] order!
        "coordinates": [[AHMEDABAD[1], AHMEDABAD[0]], [VADODARA[1], VADODARA[0]]],
    }
    if avoid_polygon:
        body["options"] = {"avoid_polygons": avoid_polygon}
    r = requests.post(
        "https://api.openrouteservice.org/v2/directions/driving-hgv/geojson",
        json=body,
        headers={**HEADERS, "Authorization": os.environ["ORS_API_KEY"]},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    s = r.json()["features"][0]["properties"]["summary"]
    return s["distance"] / 1000, s["duration"] / 60


# Small test "flood zone" near Nadiad/Anand on the corridor, as a ring of [lon, lat]
# ~1.6 km square (~6.6 km outline): public Valhalla caps exclude_polygons at 10 km.
# Real large zones are handled by our own waypoint-detour logic, not this feature.
_Z_LON, _Z_LAT, _Z_D = 72.88, 22.69, 0.008
TEST_ZONE_RING = [
    [_Z_LON - _Z_D, _Z_LAT - _Z_D], [_Z_LON + _Z_D, _Z_LAT - _Z_D],
    [_Z_LON + _Z_D, _Z_LAT + _Z_D], [_Z_LON - _Z_D, _Z_LAT + _Z_D],
    [_Z_LON - _Z_D, _Z_LAT - _Z_D],
]


def openrouteservice():
    if not os.getenv("ORS_API_KEY"):
        return "SKIPPED (no key yet - optional, Valhalla is primary)"
    zone = {"type": "Polygon", "coordinates": [TEST_ZONE_RING]}
    km1, min1 = _ors_route()
    km2, min2 = _ors_route(zone)
    return f"normal {km1:.1f} km/{min1:.0f} min | avoiding zone {km2:.1f} km/{min2:.0f} min"


def _valhalla_route(costing, exclude=None):
    body = {
        "locations": [
            {"lat": AHMEDABAD[0], "lon": AHMEDABAD[1]},
            {"lat": VADODARA[0], "lon": VADODARA[1]},
        ],
        "costing": costing,
        "directions_options": {"units": "kilometers"},
    }
    if exclude:
        body["exclude_polygons"] = [exclude]
    r = requests.post(
        "https://valhalla1.openstreetmap.de/route",
        json=body,
        headers=HEADERS,
        timeout=TIMEOUT,
    )
    _ok(r)
    s = r.json()["trip"]["summary"]
    return s["length"], s["time"] / 60


def valhalla():
    # Public FOSSGIS server: no key, but be polite (max ~1 request/sec)
    try:
        costing = "truck"
        km1, min1 = _valhalla_route(costing)
    except RuntimeError as truck_err:
        print(f"      (truck costing failed: {str(truck_err)[:150]})")
        costing = "auto"
        km1, min1 = _valhalla_route(costing)
    km2, min2 = _valhalla_route(costing, TEST_ZONE_RING)
    return (f"[{costing}] normal {km1:.1f} km/{min1:.0f} min | "
            f"avoiding zone {km2:.1f} km/{min2:.0f} min")


def osrm_fallback():
    r = requests.get(
        f"https://router.project-osrm.org/route/v1/driving/"
        f"{AHMEDABAD[1]},{AHMEDABAD[0]};{VADODARA[1]},{VADODARA[0]}",
        params={"overview": "false"},
        headers=HEADERS,
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return f"{r.json()['routes'][0]['distance'] / 1000:.1f} km"


OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]


def overpass():
    q = """
    [out:json][timeout:25];
    nwr["building"="warehouse"](22.9,72.4,23.2,72.8);
    out center 20;
    """
    errors = []
    for url in OVERPASS_MIRRORS:
        try:
            r = requests.post(url, data={"data": q}, headers=HEADERS, timeout=60)
            _ok(r)
            host = url.split("/")[2]
            return f"{len(r.json()['elements'])} real warehouses near Ahmedabad (via {host})"
        except Exception as e:
            errors.append(f"{url.split('/')[2]}: {str(e)[:60]}")
    raise RuntimeError(" | ".join(errors))


def gemini():
    from google import genai

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    model = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
    resp = client.models.generate_content(
        model=model, contents='Reply with JSON only: {"status": "ok"}'
    )
    return f"{model}: {resp.text.strip()[:60]}"


def groq_backup():
    if not os.getenv("GROQ_API_KEY"):
        return "SKIPPED (no GROQ_API_KEY yet)"
    from groq import Groq

    client = Groq(api_key=os.environ["GROQ_API_KEY"])
    # List models instead of guessing a name: proves the key works
    ids = sorted(m.id for m in client.models.list().data)
    return f"{len(ids)} models available, e.g. {ids[:3]}"


def supabase_db():
    if not os.getenv("DATABASE_URL"):
        return "SKIPPED (no DATABASE_URL yet - teammate is setting up Supabase)"
    import psycopg

    with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=10) as conn:
        v = conn.execute("select version()").fetchone()[0]
    return v[:50]


CHECKS = [
    ("Open-Meteo (weather)", open_meteo),
    ("GDACS (disasters)", gdacs),
    ("Valhalla (primary reroute)", valhalla),
    ("OpenRouteService (optional)", openrouteservice),
    ("OSRM (fallback router)", osrm_fallback),
    ("Overpass (real locations)", overpass),
    ("Gemini (LLM)", gemini),
    ("Groq (backup LLM)", groq_backup),
    ("Supabase (database)", supabase_db),
]

if __name__ == "__main__":
    failed = 0
    for name, fn in CHECKS:
        try:
            print(f"PASS  {name:30} {fn()}")
        except Exception as e:
            failed += 1
            print(f"FAIL  {name:30} {type(e).__name__}: {e}")
    print(f"\n{len(CHECKS) - failed}/{len(CHECKS)} passed")
    sys.exit(1 if failed else 0)
