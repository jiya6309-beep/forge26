"""
Hazard detection: turns live data into danger-zone polygons.

Sources (all live, no mock data):
  1. GDACS global disaster alerts (JSON API, RSS feed as fallback)
  2. Open-Meteo forecasts at our sites, scored against IMD thresholds
  3. Zones drawn by the user on the map

Each source fails independently: one outage never stops the others.
Every hazard carries its evidence (values, thresholds, source link).

Test:  python -m tools.hazards     (run from the project root)
"""
from __future__ import annotations

import math
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone

from shapely.geometry import Polygon, mapping, shape
from shapely.geometry.base import BaseGeometry

from tools.http import SourceError, request

INDIA_BBOX = (68.0, 6.0, 98.0, 37.5)  # min_lon, min_lat, max_lon, max_lat

# IMD rainfall categories, mm in 24 h
RAIN_HEAVY, RAIN_VERY_HEAVY, RAIN_EXTREME = 64.5, 115.6, 204.5
# Wind gusts, km/h
GUST_MODERATE, GUST_HIGH = 60.0, 90.0
# IMD heat wave: actual max temperature >= 45 C
HEAT_MODERATE, HEAT_HIGH = 45.0, 47.0
WEATHER_ZONE_KM = 15  # radius of the danger zone around a site with a bad forecast

SEVERITY_RANK = {"low": 0, "moderate": 1, "high": 2, "severe": 3}

GDACS_KIND = {
    "FL": "flood", "TC": "cyclone", "EQ": "earthquake",
    "WF": "wildfire", "VO": "volcano", "DR": "drought",
}
GDACS_SEVERITY = {"green": "low", "orange": "high", "red": "severe"}
# GDACS often gives only a centre point. These radii approximate the affected
# area, and are recorded as "approx_radius_km" in the evidence for transparency.
GDACS_RADIUS_KM = {
    "TC": {"low": 100, "high": 200, "severe": 300},
    "FL": {"low": 30, "high": 60, "severe": 100},
    "EQ": {"low": 30, "high": 80, "severe": 150},
    "WF": {"low": 10, "high": 20, "severe": 40},
    "VO": {"low": 20, "high": 40, "severe": 60},
    "DR": {"low": 100, "high": 200, "severe": 300},
}
GDACS_API = "https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH"
GDACS_RSS = "https://www.gdacs.org/xml/rss.xml"
RSS_NS = {
    "gdacs": "http://www.gdacs.org",
    "geo": "http://www.w3.org/2003/01/geo/wgs84_pos#",
    "georss": "http://www.georss.org/georss",
}


@dataclass
class Hazard:
    kind: str                 # flood, cyclone, heavy_rain, high_wind, heatwave, custom...
    severity: str             # low | moderate | high | severe
    title: str
    geometry: BaseGeometry    # shapely polygon, coordinates in (lon, lat)
    source: str               # GDACS | Open-Meteo | User
    evidence: dict = field(default_factory=dict)
    url: str | None = None
    valid_until: str | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    def to_feature(self) -> dict:
        """GeoJSON feature for the map."""
        return {
            "type": "Feature",
            "geometry": mapping(self.geometry),
            "properties": {
                "id": self.id, "kind": self.kind, "severity": self.severity,
                "title": self.title, "source": self.source, "url": self.url,
                "valid_until": self.valid_until, "evidence": self.evidence,
            },
        }


@dataclass
class HazardReport:
    hazards: list[Hazard]
    source_status: dict[str, str]   # source -> "ok ..." or "FAILED: ..."
    fetched_at: str


# ---------- geometry helpers ----------

def circle(lat: float, lon: float, radius_km: float, n: int = 36) -> Polygon:
    """Approximate circle in lon/lat degrees (good enough below ~500 km)."""
    km_per_deg_lat = 110.574
    km_per_deg_lon = 111.320 * math.cos(math.radians(lat))
    pts = []
    for i in range(n):
        a = 2 * math.pi * i / n
        pts.append((lon + radius_km / km_per_deg_lon * math.cos(a),
                    lat + radius_km / km_per_deg_lat * math.sin(a)))
    return Polygon(pts)


def _in_bbox(lon: float, lat: float, bbox) -> bool:
    if bbox is None:
        return True
    min_lon, min_lat, max_lon, max_lat = bbox
    return min_lon <= lon <= max_lon and min_lat <= lat <= max_lat


# ---------- source 1: GDACS ----------

def _gdacs_from_api(bbox) -> list[Hazard]:
    r = request("GET", GDACS_API, params={
        "eventlist": "EQ;TC;FL;VO;DR;WF", "alertlevel": "Green;Orange;Red",
    })
    out, seen = [], set()
    for f in r.json().get("features", []):
        p = f.get("properties") or {}
        code = p.get("eventtype")
        key = (code, p.get("eventid"))
        if code not in GDACS_KIND or key in seen:
            continue
        sev = GDACS_SEVERITY.get(str(p.get("alertlevel", "")).lower(), "low")
        geom = f.get("geometry") or {}

        if geom.get("type") == "Point":
            lon, lat = geom["coordinates"][:2]
            if not _in_bbox(lon, lat, bbox):
                continue
            radius = GDACS_RADIUS_KM[code][sev]
            poly = circle(lat, lon, radius)
        else:
            try:
                poly = shape(geom)
            except Exception:
                continue
            c = poly.centroid
            if not _in_bbox(c.x, c.y, bbox):
                continue
            radius = None
        seen.add(key)

        url = p.get("url")
        if isinstance(url, dict):
            url = url.get("report") or url.get("details")
        out.append(Hazard(
            kind=GDACS_KIND[code], severity=sev,
            title=p.get("name") or p.get("eventname") or f"GDACS {code} event",
            geometry=poly, source="GDACS", url=url, valid_until=p.get("todate"),
            evidence={
                "gdacs_event_id": p.get("eventid"),
                "alert_level": p.get("alertlevel"),
                "country": p.get("country"),
                "severity_text": (p.get("severitydata") or {}).get("severitytext"),
                "approx_radius_km": radius,
            },
        ))
    return out


def _gdacs_from_rss(bbox) -> list[Hazard]:
    r = request("GET", GDACS_RSS)
    root = ET.fromstring(r.content)
    out = []
    for item in root.iter("item"):
        code = item.findtext("gdacs:eventtype", namespaces=RSS_NS)
        if code not in GDACS_KIND:
            continue
        lat = item.findtext("geo:Point/geo:lat", namespaces=RSS_NS)
        lon = item.findtext("geo:Point/geo:long", namespaces=RSS_NS)
        if lat is None or lon is None:
            pt = item.findtext("georss:point", namespaces=RSS_NS)
            if not pt:
                continue
            lat, lon = pt.split()[:2]
        lat, lon = float(lat), float(lon)
        if not _in_bbox(lon, lat, bbox):
            continue
        level = (item.findtext("gdacs:alertlevel", namespaces=RSS_NS) or "").lower()
        sev = GDACS_SEVERITY.get(level, "low")
        radius = GDACS_RADIUS_KM[code][sev]
        out.append(Hazard(
            kind=GDACS_KIND[code], severity=sev,
            title=item.findtext("title") or f"GDACS {code} event",
            geometry=circle(lat, lon, radius), source="GDACS",
            url=item.findtext("link"),
            evidence={"alert_level": level, "approx_radius_km": radius, "via": "rss"},
        ))
    return out


def gdacs_hazards(bbox=INDIA_BBOX) -> tuple[list[Hazard], str]:
    try:
        return _gdacs_from_api(bbox), "ok (api)"
    except Exception as api_err:
        try:
            return _gdacs_from_rss(bbox), f"ok (rss fallback; api failed: {str(api_err)[:80]})"
        except Exception as rss_err:
            raise SourceError(f"GDACS api and rss both failed: {api_err} | {rss_err}")


# ---------- source 2: Open-Meteo + IMD thresholds ----------

def _rain_severity(mm):
    if mm >= RAIN_EXTREME:
        return "severe"
    if mm >= RAIN_VERY_HEAVY:
        return "high"
    if mm >= RAIN_HEAVY:
        return "moderate"
    return None


def _gust_severity(kmh):
    if kmh >= GUST_HIGH:
        return "high"
    if kmh >= GUST_MODERATE:
        return "moderate"
    return None


def _heat_severity(c):
    if c >= HEAT_HIGH:
        return "high"
    if c >= HEAT_MODERATE:
        return "moderate"
    return None


def _score_site(site: dict, daily: dict) -> list[Hazard]:
    """Worst forecast per hazard type over the next few days at one site."""
    worst = {}
    for i, day in enumerate(daily.get("time", [])):
        rain = daily["precipitation_sum"][i] or 0
        gust = daily["wind_gusts_10m_max"][i] or 0
        temp = daily["temperature_2m_max"][i] or 0
        checks = [
            ("heavy_rain", _rain_severity(rain), rain, "mm/24h", "IMD rainfall category (heavy >= 64.5 mm)"),
            ("high_wind", _gust_severity(gust), gust, "km/h gust", "gusts >= 60 km/h"),
            ("heatwave", _heat_severity(temp), temp, "C max", "IMD heat wave (max >= 45 C)"),
        ]
        for kind, sev, value, unit, basis in checks:
            if sev and (kind not in worst or SEVERITY_RANK[sev] > SEVERITY_RANK[worst[kind]["severity"]]):
                worst[kind] = {"severity": sev, "date": day, "value": value, "unit": unit, "basis": basis}

    return [
        Hazard(
            kind=kind, severity=w["severity"],
            title=f"{kind.replace('_', ' ').title()} forecast at {site['name']}",
            geometry=circle(site["lat"], site["lon"], WEATHER_ZONE_KM),
            source="Open-Meteo", url="https://open-meteo.com/", valid_until=w["date"],
            evidence={"site": site["name"], "forecast_date": w["date"],
                      "value": w["value"], "unit": w["unit"], "threshold": w["basis"]},
        )
        for kind, w in worst.items()
    ]


def weather_hazards(sites: list[dict], days: int = 3) -> list[Hazard]:
    """sites: [{"name": ..., "lat": ..., "lon": ...}]. One API call per 50 sites."""
    out = []
    for start in range(0, len(sites), 50):
        chunk = sites[start:start + 50]
        r = request("GET", "https://api.open-meteo.com/v1/forecast", params={
            "latitude": ",".join(str(s["lat"]) for s in chunk),
            "longitude": ",".join(str(s["lon"]) for s in chunk),
            "daily": "precipitation_sum,wind_gusts_10m_max,temperature_2m_max",
            "forecast_days": days,
            "timezone": "Asia/Kolkata",
        })
        data = r.json()
        if isinstance(data, dict):  # a single location returns an object, not a list
            data = [data]
        for site, forecast in zip(chunk, data):
            out.extend(_score_site(site, forecast["daily"]))
    return out


# ---------- source 3: user-drawn zones ----------

def user_zone(geojson_geometry: dict, label: str = "User-reported disruption",
              severity: str = "high") -> Hazard:
    geom = shape(geojson_geometry)
    if geom.geom_type not in ("Polygon", "MultiPolygon"):
        raise ValueError(f"Zone must be a polygon, got {geom.geom_type}")
    if not geom.is_valid:
        geom = geom.buffer(0)  # repairs self-intersecting shapes drawn by hand
    if geom.is_empty or geom.area == 0:
        raise ValueError("Zone is empty")
    return Hazard(kind="custom", severity=severity, title=label, geometry=geom,
                  source="User", evidence={"drawn_on_map": True})


# ---------- combined entry point (the agent calls this) ----------

def get_active_hazards(sites: list[dict], bbox=INDIA_BBOX,
                       user_zones: list[dict] | None = None) -> HazardReport:
    hazards, status = [], {}

    try:
        found, note = gdacs_hazards(bbox)
        hazards += found
        status["GDACS"] = f"{note}, {len(found)} events in region"
    except Exception as e:
        status["GDACS"] = f"FAILED: {str(e)[:150]}"

    try:
        found = weather_hazards(sites) if sites else []
        hazards += found
        status["Open-Meteo"] = f"ok, {len(sites)} sites checked, {len(found)} hazards"
    except Exception as e:
        status["Open-Meteo"] = f"FAILED: {str(e)[:150]}"

    for z in user_zones or []:
        try:
            hazards.append(user_zone(z))
        except ValueError as e:
            status["User zone"] = f"rejected: {e}"

    return HazardReport(
        hazards=hazards, source_status=status,
        fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


if __name__ == "__main__":
    test_sites = [
        {"name": "Ahmedabad", "lat": 23.0225, "lon": 72.5714},
        {"name": "Vadodara", "lat": 22.3072, "lon": 73.1812},
        {"name": "Surat", "lat": 21.1702, "lon": 72.8311},
        {"name": "Mumbai", "lat": 19.0760, "lon": 72.8777},
        {"name": "Chennai", "lat": 13.0827, "lon": 80.2707},
        {"name": "Kolkata", "lat": 22.5726, "lon": 88.3639},
    ]
    # A drawn zone near Nadiad, like a user would draw in the demo
    drawn = {"type": "Polygon", "coordinates": [[
        [72.80, 22.62], [72.95, 22.62], [72.95, 22.76], [72.80, 22.76], [72.80, 22.62],
    ]]}

    report = get_active_hazards(test_sites, user_zones=[drawn])
    print(f"Fetched at {report.fetched_at}\n")
    for src, st in report.source_status.items():
        print(f"  {src:12} {st}")
    print(f"\n{len(report.hazards)} hazards:")
    for h in report.hazards:
        print(f"  [{h.severity:8}] {h.kind:11} {h.source:10} {h.title[:55]}")
        print(f"             evidence: {h.evidence}")
