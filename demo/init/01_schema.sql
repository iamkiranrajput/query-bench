-- Demo database schema for Query Bench.
-- Showcases PostGIS spatial queries and pgvector semantic search.
--
-- Runs automatically on first container start via docker-entrypoint-initdb.d.

-- 1) Extensions the agent will detect via detect_extensions().
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS vector;

-- 2) Stores — each has a PostGIS point geometry (SRID 4326, lon/lat).
CREATE TABLE IF NOT EXISTS stores (
    store_id   SERIAL PRIMARY KEY,
    name       TEXT NOT NULL,
    city       TEXT,
    region     TEXT,
    geom       geometry(Point, 4326) NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stores_geom ON stores USING GIST (geom);

-- 3) Customers — each has a PostGIS home location (SRID 4326, lon/lat).
CREATE TABLE IF NOT EXISTS customers (
    customer_id SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    email       TEXT UNIQUE,
    signup_date DATE NOT NULL,
    city        TEXT,
    geom        geometry(Point, 4326)
);
CREATE INDEX IF NOT EXISTS idx_customers_geom ON customers USING GIST (geom);

-- 4) Orders — net revenue = amount - discount - refund (tax excluded).
CREATE TABLE IF NOT EXISTS orders (
    order_id    SERIAL PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(customer_id),
    order_date  DATE NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('completed','pending','cancelled','returned')),
    amount      NUMERIC(10,2) NOT NULL DEFAULT 0,
    discount    NUMERIC(10,2) NOT NULL DEFAULT 0,
    refund      NUMERIC(10,2) NOT NULL DEFAULT 0,
    tax         NUMERIC(10,2) NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders (customer_id);
CREATE INDEX IF NOT EXISTS idx_orders_status_date ON orders (status, order_date);

-- 5) Products — description embedding for pgvector semantic search.
--    all-MiniLM-L6-v2 produces 384-dim vectors; populated by seed_embeddings.py.
CREATE TABLE IF NOT EXISTS products (
    product_id  SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT NOT NULL,
    price       NUMERIC(10,2) NOT NULL DEFAULT 0,
    embedding   vector(384)
);
