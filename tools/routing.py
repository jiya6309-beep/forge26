"""
Routing + detour engine.

  get_route()        road route via Valhalla (truck), falls back to OSRM
  route_hits()       which hazards does a route cross?
  find_safe_route()  plan -> execute -> verify -> retry loop that finds a
                     route avoiding ALL hazards, of any size

Why our own detour logic? Public routers cap "avoid this area" polygons
(Valhalla: 10 km outline), but real floods and cyclones are far bigger.
We steer the route through waypoints outside the danger zone instead,
which works with any router.

Test:  python -m tools.routing     (run from the project root)
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path

from shapely.geometry import LineString, Point
from shapely.ops import unary_union

from tools.hazards import Hazard, circle
from tools.http import SourceError, request

VALHALLA_URL = "https://valhalla1.openstreetmap.de/route"
OSRM_URL = "https://router.project-osrm.org/route/v1/driving/"
KM_PER_DEG = 111.0

_last_valhalla_call = 0.0
_valhalla_costing = "truck"   # switches to "auto" once if truck is unsupported


class RoutingError(RuntimeError):
    pass


@dataclass
class Route:
    coords: list[tuple[float, float]]   # (lon, lat)
    km: float
    minutes: float
    provider: str

    @property
    def line(self) -> LineString:
        return LineString(self.coords)

    def to_feature(self, **props) -> dict:
        return {
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": self.coords},
            "properties": {"km": round(self.km, 1), "minutes": round(self.minutes),
                           "provider": self.provider, **props},
        }


@dataclass
class SafeRouteResult:
    status: str                       # clear | rerouted | endpoint_blocked | no_safe_route
    original: Route
    chosen: Route | None
    blocked_by: list[Hazard] = field(default_factory=list)
    attempts: list[dict] = field(default_factory=list)

    @property
    def extra_km(self) -> float | None:
        return None if self.chosen is None else self.chosen.km - self.original.km

    @property
    def extra_minutes(self) -> float | None:
        return None if self.chosen is None else self.chosen.minutes - self.original.minutes


# ---------- helpers ----------

def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Straight-line distance between two (lat, lon) points."""
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * 6371 * math.asin(math.sqrt(h))


def _decode_polyline6(encoded: str) -> list[tuple[float, float]]:
    """Valhalla's compressed route shape -> [(lon, lat), ...]"""
    coords, index, lat, lon = [], 0, 0, 0
    while index < len(encoded):
        deltas = []
        for _ in range(2):
            shift = result = 0
            while True:
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            deltas.append(~(result >> 1) if result & 1 else result >> 1)
        lat += deltas[0]
        lon += deltas[1]
        coords.append((lon / 1e6, lat / 1e6))
    return coords


# ---------- routing providers ----------

def _valhalla(points: list[tuple[float, float]]) -> Route:
    global _last_valhalla_call, _valhalla_costing
    wait = 1.1 - (time.time() - _last_valhalla_call)   # public server: ~1 request/sec
    if wait > 0:
        time.sleep(wait)

    body = {
        "locations": [{"lat": lat, "lon": lon} for lat, lon in points],
        "costing": _valhalla_costing,
        "directions_options": {"units": "kilometers"},
    }
    try:
        r = request("POST", VALHALLA_URL, json=body, retries=1)
    except SourceError:
        if _valhalla_costing != "truck":
            raise
        _valhalla_costing = "auto"
        body["costing"] = "auto"
        r = request("POST", VALHALLA_URL, json=body, retries=1)
    finally:
        _last_valhalla_call = time.time()

    trip = r.json()["trip"]
    coords = []
    for leg in trip["legs"]:
        coords.extend(_decode_polyline6(leg["shape"]))
    return Route(coords=coords, km=trip["summary"]["length"],
                 minutes=trip["summary"]["time"] / 60,
                 provider=f"valhalla:{_valhalla_costing}")


def _osrm(points: list[tuple[float, float]]) -> Route:
    path = ";".join(f"{lon},{lat}" for lat, lon in points)
    r = request("GET", OSRM_URL + path,
                params={"overview": "full", "geometries": "geojson"}, retries=1)
    route = r.json()["routes"][0]
    return Route(coords=[tuple(c) for c in route["geometry"]["coordinates"]],
                 km=route["distance"] / 1000, minutes=route["duration"] / 60,
                 provider="osrm:car")


PROVIDERS = [("valhalla", _valhalla), ("osrm", _osrm)]


# Routes are cached in memory and on disk. Shipments often share a corridor,
# and the public router allows ~1 request/second, so a warm cache turns a
# multi-minute run into seconds. Delete route_cache.json to start cold.
CACHE_FILE = Path(__file__).resolve().parent.parent / "route_cache.json"
SIMPLIFY_DEG = 0.0005   # ~50 m; keeps geometry accurate but much smaller


def _key(points) -> str:
    return ";".join(f"{lat:.4f},{lon:.4f}" for lat, lon in points)


def _load_cache() -> dict[str, Route]:
    try:
        raw = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        return {k: Route([tuple(c) for c in v["coords"]], v["km"], v["minutes"], v["provider"])
                for k, v in raw.items()}
    except (FileNotFoundError, ValueError, KeyError, TypeError):
        return {}


def _save_cache():
    data = {k: {"coords": r.coords, "km": r.km, "minutes": r.minutes, "provider": r.provider}
            for k, r in _route_cache.items()}
    tmp = CACHE_FILE.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(CACHE_FILE)
    except OSError:
        pass   # a read-only disk just means no persistent cache


_route_cache: dict[str, Route] = _load_cache()


def _simplified(route: Route) -> Route:
    coords = list(LineString(route.coords).simplify(SIMPLIFY_DEG).coords)
    return Route([(round(lon, 5), round(lat, 5)) for lon, lat in coords],
                 route.km, route.minutes, route.provider)


def get_route(points: list[tuple[float, float]], log: list | None = None) -> Route:
    """points: [(lat, lon), ...] origin, optional waypoints, destination."""
    key = _key(points)
    if key in _route_cache:
        return _route_cache[key]
    errors = []
    for name, fn in PROVIDERS:
        try:
            route = _simplified(fn(points))
            _route_cache[key] = route
            _save_cache()
            return route
        except Exception as e:
            errors.append(f"{name}: {str(e)[:120]}")
            if log is not None:
                log.append({"event": "provider_failed", "provider": name, "error": str(e)[:120]})
    raise RoutingError("All routing providers failed: " + " | ".join(errors))


# ---------- hazard checks ----------

def route_hits(route: Route, hazards: list[Hazard]) -> list[Hazard]:
    line = route.line
    return [h for h in hazards if line.intersects(h.geometry)]


def _flank_paths(blocking: list[Hazard], margin_km: float,
                 origin: tuple[float, float], dest: tuple[float, float],
                 n: int = 24) -> list[dict]:
    """
    Candidate detours that hug ONE side of the danger zone.

    A single waypoint only says where to go, not how to get there, so the
    router may still drive through the zone to reach it. Several waypoints
    in travel order along one flank force the whole route around the zone.
    Returns [{"side": "left"|"right", "waypoints": [(lat, lon), ...]}, ...]
    """
    zone = unary_union([h.geometry for h in blocking]).convex_hull
    ring = zone.buffer(margin_km / KM_PER_DEG).exterior
    points = [ring.interpolate(i / n, normalized=True) for i in range(n)]

    # Local flat coordinates (good enough at this scale)
    cos_lat = math.cos(math.radians(origin[0]))
    ox, oy = origin[1] * cos_lat, origin[0]
    ax, ay = dest[1] * cos_lat - ox, dest[0] - oy          # travel direction
    length = math.hypot(ax, ay) or 1e-9

    flanks = {"left": [], "right": []}
    for p in points:
        px, py = p.x * cos_lat - ox, p.y - oy
        along = (px * ax + py * ay) / length                # progress along the trip
        side = "left" if (ax * py - ay * px) > 0 else "right"
        flanks[side].append((along, (p.y, p.x)))

    options = []
    for side, pts in flanks.items():
        pts.sort()                                          # travel order
        ordered = [latlon for _, latlon in pts]
        if not ordered:
            continue
        for k in (1, 2, 3, 5):
            if k >= len(ordered):
                chosen = ordered
            elif k == 1:
                chosen = [ordered[len(ordered) // 2]]       # point beside the zone
            else:
                step = (len(ordered) - 1) / (k - 1)
                chosen = [ordered[round(i * step)] for i in range(k)]
            options.append({"side": side, "waypoints": chosen})
            if len(chosen) == len(ordered):
                break
    return options


def _path_line(points: list[tuple[float, float]]) -> LineString:
    return LineString([(lon, lat) for lat, lon in points])


def _path_km(points: list[tuple[float, float]]) -> float:
    return sum(haversine_km(points[i], points[i + 1]) for i in range(len(points) - 1))


# ---------- the detour loop ----------

def find_safe_route(origin: tuple[float, float], dest: tuple[float, float],
                    hazards: list[Hazard], margin_km: float = 5,
                    tries_per_round: int = 4, rounds: int = 2,
                    original: Route | None = None) -> SafeRouteResult:
    """original: pass the base route if you already have it (saves an API call)."""
    log: list[dict] = []
    if original is None:
        original = get_route([origin, dest], log)
    blocking = route_hits(original, hazards)
    log.append({"event": "base_route", "km": round(original.km, 1),
                "provider": original.provider,
                "crosses": [h.title for h in blocking]})

    if not blocking:
        return SafeRouteResult("clear", original, original, [], log)

    # A detour can't help if the trip starts or ends inside the danger zone
    for label, (lat, lon) in (("origin", origin), ("destination", dest)):
        inside = [h for h in blocking if h.geometry.contains(Point(lon, lat))]
        if inside:
            log.append({"event": "endpoint_blocked", "which": label,
                        "zone": inside[0].title})
            return SafeRouteResult("endpoint_blocked", original, None, blocking, log)

    best: Route | None = None
    zone = unary_union([h.geometry for h in blocking])
    for rnd in range(1, rounds + 1):
        options = _flank_paths(blocking, margin_km, origin, dest)
        # Drop options with a waypoint inside any hazard
        options = [o for o in options
                   if not any(h.geometry.contains(Point(w[1], w[0]))
                              for w in o["waypoints"] for h in hazards)]
        # Free pre-check (no API call): the straight-line path through the
        # waypoints should already clear the zone. Filters out doomed tries.
        clear_path = [o for o in options
                      if not _path_line([origin, *o["waypoints"], dest]).intersects(zone)]
        log.append({"event": "plan", "round": rnd, "margin_km": margin_km,
                    "options": len(options), "pass_precheck": len(clear_path)})
        options = clear_path or options
        # Most promising first: shortest straight-line path
        options.sort(key=lambda o: _path_km([origin, *o["waypoints"], dest]))

        for opt in options[:tries_per_round]:
            entry = {"event": "detour_attempt", "round": rnd, "margin_km": margin_km,
                     "side": opt["side"], "n_waypoints": len(opt["waypoints"]),
                     "waypoints": [(round(a, 3), round(b, 3)) for a, b in opt["waypoints"]]}
            try:
                candidate = get_route([origin, *opt["waypoints"], dest], log)
            except RoutingError as e:
                entry.update(result="routing_failed", error=str(e)[:120])
                log.append(entry)
                continue

            hits = route_hits(candidate, hazards)
            entry.update(km=round(candidate.km, 1), provider=candidate.provider,
                         result="rejected" if hits else "clear",
                         crosses=[h.title for h in hits])
            log.append(entry)
            if not hits and (best is None or candidate.km < best.km):
                best = candidate
                break   # shortest-first ordering: first clear option is good enough

        if best:
            log.append({"event": "chosen", "km": round(best.km, 1),
                        "extra_km": round(best.km - original.km, 1)})
            return SafeRouteResult("rerouted", original, best, blocking, log)

        if rnd < rounds:
            log.append({"event": "self_correct",
                        "reason": f"no clear detour at {margin_km} km margin",
                        "next_margin_km": margin_km * 2})
            margin_km *= 2
        else:
            log.append({"event": "give_up", "rounds": rounds,
                        "reason": f"no clear detour up to {margin_km} km margin"})

    return SafeRouteResult("no_safe_route", original, None, blocking, log)


if __name__ == "__main__":
    AHMEDABAD, VADODARA = (23.0225, 72.5714), (22.3072, 73.1812)

    base = get_route([AHMEDABAD, VADODARA])
    print(f"Base route: {base.km:.1f} km, {base.minutes:.0f} min via {base.provider}")

    # Put a 12 km flood zone exactly on the real route's midpoint
    mid_lon, mid_lat = base.coords[len(base.coords) // 2]
    flood = Hazard(kind="custom", severity="high", title="Test flood zone (on route)",
                   geometry=circle(mid_lat, mid_lon, 12), source="User")

    result = find_safe_route(AHMEDABAD, VADODARA, [flood])
    print(f"\nStatus: {result.status}")
    for step in result.attempts:
        print("  ", step)
    if result.chosen:
        print(f"\nSafe route: {result.chosen.km:.1f} km "
              f"(+{result.extra_km:.1f} km, +{result.extra_minutes:.0f} min)")
