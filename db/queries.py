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

ACTIVE = ("in_transit", "at_risk")


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
