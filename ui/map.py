"""
The live map: dark basemap, hazard zones, shipments, and animated safe routes.
"""
import math

import folium
from folium.plugins import AntPath, Draw

# Esri's dark canvas needs no API key (CartoDB's dark tiles now do).
DARK_TILES = ("https://server.arcgisonline.com/ArcGIS/rest/services/"
              "Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}")
DARK_LABELS = ("https://server.arcgisonline.com/ArcGIS/rest/services/"
               "Canvas/World_Dark_Gray_Reference/MapServer/tile/{z}/{y}/{x}")

# Flip to False if the animated routes ever misbehave during a demo.
ANIMATE_ROUTES = True

TEAL, ROSE, BLUE, PURPLE = "#2DD4BF", "#FB7185", "#60A5FA", "#A78BFA"
SEVERITY = {"low": "#FDE68A", "moderate": "#FBBF24", "high": "#F59E0B", "severe": "#EA580C"}


def view_for(points, width_px=600, height_px=560):
    """Centre and zoom that fit every point.

    We work this out ourselves because Leaflet's fitBounds runs before the
    map has its final size inside Streamlit and zooms out to the whole world.
    """
    if not points:
        return [22.0, 72.8], 7
    lats, lons = [p[0] for p in points], [p[1] for p in points]
    lat_span = max(max(lats) - min(lats), 0.5) * 1.2
    lon_span = max(max(lons) - min(lons), 0.5) * 1.2
    zoom = min(math.log2(width_px * 360 / (256 * lon_span)),
               math.log2(height_px * 180 / (256 * lat_span)))
    zoom = int(max(4, min(10, math.floor(zoom))))
    return [(min(lats) + max(lats)) / 2, (min(lons) + max(lons)) / 2], zoom


def _safe_route(m, route_geojson, tooltip):
    latlons = [(lat, lon) for lon, lat in route_geojson["geometry"]["coordinates"]]
    if ANIMATE_ROUTES:
        AntPath(latlons, color=TEAL, pulse_color="#CCFBF1", weight=5, opacity=0.95,
                delay=900, dash_array=[14, 22], tooltip=tooltip).add_to(m)
    else:
        folium.PolyLine(latlons, color=TEAL, weight=5, opacity=0.9, tooltip=tooltip).add_to(m)


def build_map(warehouses, suppliers, shipments, pending_orders, hazards, zones):
    center, zoom = view_for([(w["lat"], w["lon"]) for w in warehouses]
                            + [(s["lat"], s["lon"]) for s in suppliers]
                            + [(s["dest_lat"], s["dest_lon"]) for s in shipments])

    m = folium.Map(location=center, zoom_start=zoom, tiles=None, control_scale=True)
    folium.TileLayer(DARK_TILES, attr="Tiles © Esri", name="Dark").add_to(m)
    folium.TileLayer(DARK_LABELS, attr="Labels © Esri", overlay=True, control=False).add_to(m)

    for h in hazards:
        p = h["properties"]
        color = PURPLE if p["source"] == "User" else SEVERITY.get(p["severity"], "#F59E0B")
        folium.GeoJson(
            h,
            style_function=lambda f, c=color: {"color": c, "fillColor": c, "fillOpacity": 0.22,
                                               "weight": 2, "dashArray": "6,5"},
            tooltip=f"{p['title']} · {p['source']} · {p['severity']}",
        ).add_to(m)

    # Zones the user drew or loaded but hasn't run yet
    for z in zones:
        folium.GeoJson(
            {"type": "Feature", "geometry": z, "properties": {}},
            style_function=lambda f: {"color": "#F59E0B", "fillColor": "#F59E0B",
                                      "fillOpacity": 0.15, "weight": 2, "dashArray": "6,5"},
            tooltip="Your disruption zone",
        ).add_to(m)

    for s in shipments:
        origin, dest = (s["origin_lat"], s["origin_lon"]), (s["dest_lat"], s["dest_lon"])
        tip = f"#{s['id']} {s['origin']} → {s['destination']} · {s['qty']} × {s['sku']} · {s['status']}"
        if s["route_geojson"]:
            folium.PolyLine([origin, dest], color=ROSE, weight=1.5, dash_array="4,8", opacity=0.55).add_to(m)
            _safe_route(m, s["route_geojson"], tip + " · safe route")
        else:
            color = ROSE if s["status"] in ("at_risk", "delayed") else BLUE
            folium.PolyLine([origin, dest], color=color, weight=2, dash_array="7,7",
                            opacity=0.8, tooltip=tip).add_to(m)
        folium.CircleMarker(dest, radius=4, color="#E6F1EF", weight=1, fill=True,
                            fill_opacity=0.9, tooltip=s["destination"]).add_to(m)

    for order in pending_orders:
        folium.PolyLine([(order["sup_lat"], order["sup_lon"]), (order["dest_lat"], order["dest_lon"])],
                        color=TEAL, weight=3, dash_array="2,9",
                        tooltip=f"PO-{order['id']}: {order['supplier']} → {order['dest_name']}").add_to(m)

    for w in warehouses:
        folium.CircleMarker((w["lat"], w["lon"]), radius=9, color="#0A1214", weight=3, fill=True,
                            fill_color=BLUE, fill_opacity=1, tooltip=f"Warehouse: {w['name']}").add_to(m)
    for s in suppliers:
        folium.CircleMarker((s["lat"], s["lon"]), radius=6, color="#0A1214", weight=2, fill=True,
                            fill_color=TEAL, fill_opacity=0.95,
                            tooltip=f"Supplier: {s['name']} | {s['stock']}").add_to(m)

    Draw(export=False,
         draw_options={"polyline": False, "circle": False, "marker": False,
                       "circlemarker": False, "polygon": True, "rectangle": True},
         edit_options={"edit": False}).add_to(m)
    return m, center, zoom
