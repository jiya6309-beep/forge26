-- Run in Supabase: SQL Editor -> New query -> paste -> Run

create table if not exists warehouses (
    id          serial primary key,
    name        text not null,
    lat         double precision not null,
    lon         double precision not null,
    osm_id      text unique,              -- real location from OpenStreetMap
    created_at  timestamptz default now()
);

create table if not exists suppliers (
    id          serial primary key,
    name        text not null,
    lat         double precision not null,
    lon         double precision not null,
    osm_id      text unique,
    created_at  timestamptz default now()
);

create table if not exists inventory (
    supplier_id int references suppliers(id) on delete cascade,
    sku         text not null,
    qty         int not null check (qty >= 0),
    updated_at  timestamptz default now(),
    primary key (supplier_id, sku)
);

create table if not exists shipments (
    id            serial primary key,
    sku           text not null,
    qty           int not null,
    origin_id     int references warehouses(id),
    dest_name     text not null,
    dest_lat      double precision not null,
    dest_lon      double precision not null,
    status        text default 'in_transit',   -- in_transit | at_risk | rerouted | delayed
    route_geojson jsonb,
    created_at    timestamptz default now()
);

create table if not exists purchase_orders (
    id          serial primary key,
    supplier_id int references suppliers(id),
    shipment_id int references shipments(id),
    sku         text not null,
    qty         int not null,
    status      text default 'pending_approval', -- pending_approval | approved | sent | rejected
    reasoning   text,                            -- agent's explanation + sources
    created_at  timestamptz default now()
);

create table if not exists agent_runs (
    id          serial primary key,
    started_at  timestamptz default now(),
    trigger     text,                            -- 'live_alert' | 'user_zone' | 'manual'
    trace       jsonb default '[]'::jsonb,       -- every step, tool call, retry
    status      text default 'running'
);
