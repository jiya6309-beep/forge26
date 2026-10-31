"""
Database access for the agent (Supabase Postgres, see db/schema.sql).
Every function opens a short connection, so a dropped connection
never leaves the app in a broken state.
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

ACTIVE = ("in_transit",)   # shipments awaiting approval are not re-planned


@contextmanager
def _conn():
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    # prepare_threshold=None: required for Supabase's pooler (port 6543)
    with psycopg.connect(url, row_factory=dict_row, prepare_threshold=None,
                         connect_timeout=10) as conn:
        yield conn          # commits on success, rolls back on error


def _json(obj):
    return Jsonb(obj, dumps=lambda o: json.dumps(o, default=str))


def get_sites() -> list[dict]:
    """Every place we care about the weather at: warehouses, suppliers, destinations."""
    with _conn() as c:
        rows = c.execute("""
            select name, lat, lon from warehouses
            union select name, lat, lon from suppliers
            union select dest_name, dest_lat, dest_lon from shipments
                  where status = any(%s)
        """, (list(ACTIVE),)).fetchall()
    return rows


def get_active_shipments() -> list[dict]:
    with _conn() as c:
        return c.execute("""
            select s.id, s.sku, s.qty, s.status,
                   s.dest_name, s.dest_lat, s.dest_lon,
                   w.id as origin_id, w.name as origin_name,
                   w.lat as origin_lat, w.lon as origin_lon
            from shipments s
            join warehouses w on w.id = s.origin_id
            where s.status = any(%s)
            order by s.id
        """, (list(ACTIVE),)).fetchall()


def get_suppliers_with_stock(sku: str, min_qty: int) -> list[dict]:
    with _conn() as c:
        return c.execute("""
            select su.id, su.name, su.lat, su.lon, i.qty
            from suppliers su
            join inventory i on i.supplier_id = su.id
            where i.sku = %s and i.qty >= %s
        """, (sku, min_qty)).fetchall()


def set_shipment_status(shipment_id: int, status: str, route_geojson: dict | None = None):
    with _conn() as c:
        c.execute("""
            update shipments
            set status = %s, route_geojson = coalesce(%s, route_geojson)
            where id = %s
        """, (status, _json(route_geojson) if route_geojson else None, shipment_id))


def create_purchase_order(supplier_id: int, shipment_id: int, sku: str,
                          qty: int, reasoning: str) -> int:
    with _conn() as c:
        row = c.execute("""
            insert into purchase_orders (supplier_id, shipment_id, sku, qty, reasoning)
            values (%s, %s, %s, %s, %s)
            returning id
        """, (supplier_id, shipment_id, sku, qty, reasoning)).fetchone()
    return row["id"]


def save_agent_run(trigger: str, trace: list, status: str) -> int:
    with _conn() as c:
        row = c.execute("""
            insert into agent_runs (trigger, trace, status)
            values (%s, %s, %s)
            returning id
        """, (trigger, _json(trace), status)).fetchone()
    return row["id"]


# ---------------- used by the UI ----------------

def get_warehouses() -> list[dict]:
    with _conn() as c:
        return c.execute("select id, name, lat, lon from warehouses order by id").fetchall()


def get_suppliers() -> list[dict]:
    with _conn() as c:
        return c.execute("""
            select su.id, su.name, su.lat, su.lon,
                   coalesce(string_agg(i.sku || ': ' || i.qty, ', ' order by i.sku), '') as stock
            from suppliers su
            left join inventory i on i.supplier_id = su.id
            group by su.id order by su.id
        """).fetchall()


def get_all_shipments() -> list[dict]:
    with _conn() as c:
        return c.execute("""
            select s.id, w.name as origin, s.dest_name as destination, s.sku, s.qty,
                   s.status, w.lat as origin_lat, w.lon as origin_lon,
                   s.dest_lat, s.dest_lon, s.route_geojson
            from shipments s
            join warehouses w on w.id = s.origin_id
            order by s.id
        """).fetchall()


def get_pending_reroutes() -> list[dict]:
    with _conn() as c:
        return c.execute("""
            select s.id, w.name as origin, s.dest_name as destination, s.sku, s.qty,
                   s.route_geojson
            from shipments s
            join warehouses w on w.id = s.origin_id
            where s.status = 'at_risk'
              and s.route_geojson -> 'properties' ->> 'proposed' = 'true'
            order by s.id
        """).fetchall()


def get_pending_pos() -> list[dict]:
    with _conn() as c:
        return c.execute("""
            select po.id, po.sku, po.qty, po.reasoning, po.shipment_id,
                   su.name as supplier, su.lat as sup_lat, su.lon as sup_lon,
                   s.dest_name, s.dest_lat, s.dest_lon
            from purchase_orders po
            join suppliers su on su.id = po.supplier_id
            join shipments s on s.id = po.shipment_id
            where po.status = 'pending_approval'
            order by po.id
        """).fetchall()


def approve_reroute(shipment_id: int):
    with _conn() as c:
        c.execute("""
            update shipments
            set status = 'rerouted',
                route_geojson = jsonb_set(route_geojson, '{properties,proposed}', 'false'::jsonb)
            where id = %s and status = 'at_risk'
        """, (shipment_id,))


def reject_reroute(shipment_id: int):
    with _conn() as c:
        c.execute("update shipments set status = 'in_transit', route_geojson = null where id = %s",
                  (shipment_id,))


def approve_po(po_id: int) -> bool:
    """Approve an order: deduct supplier stock and mark the shipment re-sourced, atomically."""
    with _conn() as c:
        po = c.execute("""
            update purchase_orders set status = 'approved'
            where id = %s and status = 'pending_approval'
            returning supplier_id, sku, qty, shipment_id
        """, (po_id,)).fetchone()
        if not po:
            return False
        c.execute("""
            update inventory set qty = greatest(qty - %s, 0), updated_at = now()
            where supplier_id = %s and sku = %s
        """, (po["qty"], po["supplier_id"], po["sku"]))
        c.execute("update shipments set status = 'resourced' where id = %s", (po["shipment_id"],))
    return True


def reject_po(po_id: int):
    with _conn() as c:
        po = c.execute("""
            update purchase_orders set status = 'rejected'
            where id = %s returning shipment_id
        """, (po_id,)).fetchone()
        if po:
            c.execute("update shipments set status = 'in_transit' where id = %s",
                      (po["shipment_id"],))


def add_shipment(origin_id: int, sku: str, qty: int, dest_name: str,
                 dest_lat: float, dest_lon: float) -> int:
    with _conn() as c:
        row = c.execute("""
            insert into shipments (sku, qty, origin_id, dest_name, dest_lat, dest_lon)
            values (%s, %s, %s, %s, %s, %s) returning id
        """, (sku, qty, origin_id, dest_name, dest_lat, dest_lon)).fetchone()
    return row["id"]


def reset_demo():
    """Undo all agent actions so the demo can be run again from a clean state."""
    with _conn() as c:
        c.execute("""
            update inventory i set qty = i.qty + po.qty
            from purchase_orders po
            where po.status = 'approved' and po.supplier_id = i.supplier_id and po.sku = i.sku
        """)
        c.execute("delete from purchase_orders")
        c.execute("update shipments set status = 'in_transit', route_geojson = null")
